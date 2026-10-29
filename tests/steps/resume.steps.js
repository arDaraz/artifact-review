import { randomUUID } from 'node:crypto';
import { Given, When, Then, expect } from '../support/bdd.js';
import { ROOT } from '../support/arev.js';

// No pid means no process to check, so the server treats the session as closed.
Given('the agent session that opened the review is recorded', async ({ arev }) => {
  await arev.api('POST', '/agent-session',
    { agent:'claude-code', session_id:randomUUID(), cwd:ROOT, pid:null });
});

Given('the agent session that opened the review is still running', async ({ arev }) => {
  await arev.api('POST', '/agent-session',
    { agent:'claude-code', session_id:randomUUID(), cwd:ROOT, pid:process.pid });
});

When('the agent reports that it is listening', async ({ arev }) => {
  await arev.api('POST', '/agent-status', { status:'listening' });
});

When('the reviewer chooses to resume the agent', async ({ rail }) => {
  await rail.resume.click();
});

Then('the resume action is dimmed', async ({ rail }) => {
  await expect(rail.resume).toBeDisabled();
});

Then('the resume action is available', async ({ rail }) => {
  await expect(rail.resume).toBeEnabled();
});

Then('the reviewer is told the agent session is still open', async ({ rail }) => {
  await expect(rail.banner).toContainText('still open in another terminal');
});
