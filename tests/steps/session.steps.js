import { Given, Then, expect } from '../support/bdd.js';

// The fixtures are listed so other "a ... artifact" steps cannot collide here.
Given(/^a (broken|clean|diagram-features|many-diagrams|mermaid-broken|scrolling-table|themed|viewport-overflow) artifact$/,
  async ({ artifact }, kind) => {
    await artifact.from(`${kind}.html`);
  });

Given('the review server is running', async ({ arev }) => {
  await arev.open();
});

// The layout gate may block this session, so this step does not wait for the curtain.
Given('the reviewer opens the review session', async ({ arev, page }) => {
  await arev.open();
  await page.goto(arev.sessionUrl, { waitUntil:'domcontentloaded' });
});

Given('the reviewer has the review session open', async ({ arev, page, rail }) => {
  // A scenario that already started the server only needs the browser.
  if (!arev.sessionUrl) await arev.open();
  await page.goto(arev.sessionUrl, { waitUntil:'domcontentloaded' });
  await expect(rail.curtain).toBeHidden();
});

Then('the artifact is visible in the review surface', async ({ rail }) => {
  await expect(rail.artifact.locator('body')).not.toBeEmpty();
});

Then('the review rail is ready for feedback', async ({ rail }) => {
  await expect(rail.chat).toBeEnabled();
  await expect(rail.chatAction).toBeEnabled();
  await expect(rail.queueCount).toHaveText('0');
});

Then('the review server reports a healthy session', async ({ arev }) => {
  const health = await arev.api('GET', '/health');
  expect(health.instance_id).toMatch(/^[a-f0-9-]{36}$/);
  expect(health.tool_version).toMatch(/^\d+\.\d+\.\d+$/);
  expect(health.event_schema).toMatch(/^artifact-review\/event\/v\d+$/);
});
