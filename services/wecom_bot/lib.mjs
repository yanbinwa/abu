import fs from 'node:fs';
import path from 'node:path';

export function parseEnvFile(text) {
  const result = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const separator = line.indexOf('=');
    if (separator < 1) throw new Error(`Invalid environment line: ${rawLine}`);
    const key = line.slice(0, separator).trim();
    let value = line.slice(separator + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    result[key] = value;
  }
  return result;
}

export function loadLocalEnv(filePath) {
  if (!fs.existsSync(filePath)) return;
  const values = parseEnvFile(fs.readFileSync(filePath, 'utf8'));
  for (const [key, value] of Object.entries(values)) {
    if (process.env[key] === undefined) process.env[key] = value;
  }
}

export function atomicWriteJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, filePath);
}

export function loadState(filePath) {
  try {
    const value = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    return {
      ownerUserId: typeof value.ownerUserId === 'string' ? value.ownerUserId : null,
      seenMessageIds: Array.isArray(value.seenMessageIds) ? value.seenMessageIds.slice(-500) : [],
      lastConversation: value.lastConversation ?? null,
    };
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
    return { ownerUserId: null, seenMessageIds: [], lastConversation: null };
  }
}

export function rememberMessage(state, messageId) {
  if (!messageId || state.seenMessageIds.includes(messageId)) return false;
  state.seenMessageIds.push(messageId);
  if (state.seenMessageIds.length > 500) state.seenMessageIds.splice(0, state.seenMessageIds.length - 500);
  return true;
}

export function normalizeCommand(content) {
  return content.replace(/^\s*@\S+\s*/, '').trim();
}

export function reportExcerpt(report, symbol) {
  const normalized = symbol.toLowerCase();
  const bare = normalized.replace(/^(sh|sz|bj)/, '');
  const lines = report.split(/\r?\n/);
  const matches = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].toLowerCase();
    if (line.includes(normalized) || (bare.length === 6 && line.includes(bare))) {
      matches.push(...lines.slice(Math.max(0, index - 1), Math.min(lines.length, index + 2)));
    }
  }
  return [...new Set(matches)].slice(0, 12).join('\n').trim();
}

export function routeText({ content, userId, chatType, state, bindCode, report, reportMtime }) {
  const command = normalizeCommand(content);
  const binding = command.match(/^绑定(?:\s+(.+))?$/);
  if (!state.ownerUserId) {
    if (chatType !== 'single') return { ignore: true };
    if (!binding || binding[1] !== bindCode) {
      return { text: '机器人尚未绑定。请发送“绑定 配对码”。' };
    }
    state.ownerUserId = userId;
    return {
      persist: true,
      text: '绑定成功。以后可以发送“今日策略”“解释 600000”“状态”或“帮助”。',
    };
  }
  if (userId !== state.ownerUserId) return { ignore: true };

  state.lastConversation = { userId, chatType, updatedAt: new Date().toISOString() };
  const persist = true;
  if (/^(帮助|help|\?)$/i.test(command)) {
    return { persist, text: [
      '可用命令：',
      '• 今日策略／今日信号',
      '• 解释 600000',
      '• 状态',
      '• 推送测试',
    ].join('\n') };
  }
  if (/^(今日策略|今日信号|策略)$/i.test(command)) {
    return { persist, text: report || '今天的策略报告尚未生成。' };
  }
  if (/^状态$/i.test(command)) {
    const reportStatus = reportMtime ? `策略报告更新时间：${reportMtime}` : '策略报告：尚未生成';
    return { persist, text: `企业微信机器人连接正常。\n${reportStatus}` };
  }
  if (/^推送测试$/i.test(command)) {
    return { persist, text: '交互链路正常。主动推送会使用同一个企业微信机器人。' };
  }
  const explanation = command.match(/^(?:解释|查询|分析)\s*((?:sh|sz|bj)?\d{6})$/i);
  if (explanation) {
    if (!report) return { persist, text: '今天的策略报告尚未生成，暂时没有可解释的信号。' };
    const excerpt = reportExcerpt(report, explanation[1]);
    return { persist, text: excerpt || `今天的策略报告中没有找到 ${explanation[1]}。` };
  }
  return { persist, text: '我暂时只处理策略日报查询。发送“帮助”查看可用命令。' };
}
