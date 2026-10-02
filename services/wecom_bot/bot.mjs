#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import AiBot, { generateReqId } from '@wecom/aibot-node-sdk';

import {
  atomicWriteJson, loadLocalEnv, loadState, rememberMessage, routeText,
} from './lib.mjs';

const serviceDir = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(serviceDir, '../..');
loadLocalEnv(path.join(rootDir, '.env.wecom'));

const botId = process.env.WECOM_BOT_ID;
const secret = process.env.WECOM_BOT_SECRET;
const bindCode = process.env.WECOM_BIND_CODE;
if (!botId || !secret || !bindCode) {
  console.error('Missing WECOM_BOT_ID, WECOM_BOT_SECRET or WECOM_BIND_CODE');
  process.exit(2);
}

const runtimeDir = path.resolve(rootDir, process.env.WECOM_RUNTIME_DIR || 'runtime/wecom');
const statePath = path.join(runtimeDir, 'state.json');
const statusPath = path.join(runtimeDir, 'status.json');
const outboxDir = path.join(runtimeDir, 'outbox');
const reportPath = path.resolve(rootDir, process.env.WECOM_REPORT_FILE || 'runtime/daily_strategy.md');
fs.mkdirSync(outboxDir, { recursive: true });
const state = loadState(statePath);
let authenticated = false;
let draining = false;

function safeLog(level, message) {
  const cleaned = String(message).replaceAll(secret, '[secret]');
  process.stderr.write(`${new Date().toISOString()} ${level} ${cleaned}\n`);
}

function saveState() {
  atomicWriteJson(statePath, state);
}

function updateStatus(extra = {}) {
  atomicWriteJson(statusPath, {
    connected: authenticated,
    ownerBound: Boolean(state.ownerUserId),
    updatedAt: new Date().toISOString(),
    ...extra,
  });
}

function readReport() {
  try {
    const stats = fs.statSync(reportPath);
    return {
      content: fs.readFileSync(reportPath, 'utf8').trim(),
      mtime: stats.mtime.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }),
    };
  } catch (error) {
    if (error?.code !== 'ENOENT') safeLog('WARN', `读取策略报告失败：${error?.message}`);
    return { content: '', mtime: null };
  }
}

const wsClient = new AiBot.WSClient({
  botId,
  secret,
  maxReconnectAttempts: -1,
  logger: {
    debug: () => {},
    info: (message) => safeLog('INFO', message),
    warn: (message) => safeLog('WARN', message),
    error: (message) => safeLog('ERROR', message),
  },
});

wsClient.on('authenticated', () => {
  authenticated = true;
  updateStatus({ lastAuthenticatedAt: new Date().toISOString() });
  safeLog('INFO', '企业微信智能机器人认证成功');
});

wsClient.on('disconnected', () => {
  authenticated = false;
  updateStatus();
});

wsClient.on('message.text', async (frame) => {
  const body = frame.body || {};
  if (!rememberMessage(state, body.msgid)) return;
  saveState();
  const report = readReport();
  const result = routeText({
    content: body.text?.content || '',
    userId: body.from?.userid || '',
    chatType: body.chattype || 'single',
    state,
    bindCode,
    report: report.content,
    reportMtime: report.mtime,
  });
  if (result.persist) saveState();
  if (result.ignore || !result.text) return;
  try {
    await wsClient.replyStream(frame, generateReqId('reply'), result.text, true);
  } catch (error) {
    safeLog('ERROR', `回复失败：${error?.message || error}`);
  }
});

async function drainOutbox() {
  if (!authenticated || draining) return;
  draining = true;
  try {
    const jobs = fs.readdirSync(outboxDir).filter((name) => name.endsWith('.json')).sort();
    for (const name of jobs) {
      const jobPath = path.join(outboxDir, name);
      let job;
      try {
        job = JSON.parse(fs.readFileSync(jobPath, 'utf8'));
        const target = job.target || state.ownerUserId;
        if (!target) throw new Error('尚未绑定接收用户');
        if (!job.content || typeof job.content !== 'string') throw new Error('推送内容为空');
        await wsClient.sendMessage(target, {
          msgtype: 'markdown',
          markdown: { content: job.content },
        });
        fs.renameSync(jobPath, path.join(runtimeDir, `sent-${name}`));
        updateStatus({ lastPushAt: new Date().toISOString(), lastPushId: job.id || name });
      } catch (error) {
        safeLog('ERROR', `推送任务 ${name} 失败：${error?.message || error}`);
        break;
      }
    }
  } finally {
    draining = false;
  }
}

const timer = setInterval(drainOutbox, 2000);
timer.unref();
updateStatus({ starting: true });
wsClient.connect();

function shutdown() {
  clearInterval(timer);
  authenticated = false;
  updateStatus({ stoppedAt: new Date().toISOString() });
  wsClient.disconnect();
  setTimeout(() => process.exit(0), 100).unref();
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
