import test from 'node:test';
import assert from 'node:assert/strict';

import { parseEnvFile, rememberMessage, reportExcerpt, routeText } from '../lib.mjs';

test('parses local environment without evaluating shell syntax', () => {
  assert.deepEqual(parseEnvFile('A=one\nB="two words"\n# comment\n'), { A: 'one', B: 'two words' });
});

test('binds only a direct user with the correct code', () => {
  const state = { ownerUserId: null, seenMessageIds: [], lastConversation: null };
  assert.equal(routeText({ content: '绑定 wrong', userId: 'u1', chatType: 'single', state,
    bindCode: '123456', report: '', reportMtime: null }).persist, undefined);
  const result = routeText({ content: '绑定 123456', userId: 'u1', chatType: 'single', state,
    bindCode: '123456', report: '', reportMtime: null });
  assert.equal(result.persist, true);
  assert.equal(state.ownerUserId, 'u1');
});

test('ignores other users after binding', () => {
  const state = { ownerUserId: 'owner', seenMessageIds: [], lastConversation: null };
  const result = routeText({ content: '今日策略', userId: 'other', chatType: 'single', state,
    bindCode: '123456', report: 'report', reportMtime: null });
  assert.equal(result.ignore, true);
});

test('returns the report and symbol excerpt', () => {
  const state = { ownerUserId: 'owner', seenMessageIds: [], lastConversation: null };
  const report = '# 今日策略\nsh600000 买入观察\n突破 20 日新高';
  assert.equal(routeText({ content: '今日策略', userId: 'owner', chatType: 'single', state,
    bindCode: '123456', report, reportMtime: null }).text, report);
  assert.match(reportExcerpt(report, '600000'), /突破 20 日新高/);
});

test('deduplicates message ids', () => {
  const state = { seenMessageIds: [] };
  assert.equal(rememberMessage(state, 'm1'), true);
  assert.equal(rememberMessage(state, 'm1'), false);
});
