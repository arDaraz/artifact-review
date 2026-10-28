import { Given, When, Then, expect } from '../support/bdd.js';

const FIRST = 'well-formed paragraph';
const SECOND = 'second paragraph';

Given('the reviewer has turned edit text mode on', async ({ rail }) => {
  if (await rail.editToggle.getAttribute('aria-pressed') !== 'true') {
    await rail.editToggle.click();
  }
  await expect(rail.editToggle).toHaveAttribute('aria-pressed', 'true');
});

When('the reviewer toggles edit text mode', async ({ rail }) => {
  await rail.editToggle.click();
});

Then(/^edit text mode is (on|off)$/, async ({ rail }, state) => {
  await expect(rail.editToggle)
    .toHaveAttribute('aria-pressed', String(state === 'on'));
});

When(/^the reviewer points at "([^"]*)"$/, async ({ editor }, text) => {
  await editor.block(text).hover();
  await expect(editor.handles).toBeVisible();
});

Then(/^the line handles read "([^"]*)" and "([^"]*)"$/,
  async ({ editor }, pencil, bin) => {
    await expect(editor.handles.locator('button')).toHaveCount(2);
    await expect(editor.frame.locator(`button[aria-label="${pencil}"]`)).toBeVisible();
    await expect(editor.frame.locator(`button[aria-label="${bin}"]`)).toBeVisible();
  });

When('the reviewer clicks the pencil handle', async ({ editor }) => {
  await editor.editHandle.click();
});

When('the reviewer clicks the bin handle', async ({ editor }) => {
  await editor.cutHandle.click();
});

When(/^the reviewer clicks the line "([^"]*)"$/, async ({ editor }, text) => {
  await editor.block(text).click({ position:{ x:5, y:5 } });
  await expect(editor.editor).toBeVisible();
});

Then(/^the editor holds "([^"]*)"$/, async ({ editor }, text) => {
  await expect(editor.editor).toHaveText(text);
});

Then('the editor is the paragraph itself', async ({ editor }) => {
  await expect(editor.frame.locator('p.arev-live-edit')).toHaveCount(1);
});

Then('the editor holds every line of the range', async ({ editor }) => {
  await expect(editor.editor).toContainText('well-formed paragraph');
  await expect(editor.editor).toContainText('second paragraph');
});

When(/^the reviewer types "([^"]*)"$/, async ({ editor }, text) => {
  await editor.type(text);
});

When('the reviewer saves the edit', async ({ editor }) => {
  await editor.save.click();
  await expect(editor.editor).toHaveCount(0);
});

When('the reviewer cancels the edit', async ({ editor }) => {
  await editor.cancel.click();
  await expect(editor.editor).toHaveCount(0);
});

When(/^the reviewer selects "([^"]*)" inside "([^"]*)"$/,
  async ({ editor }, phrase, block) => {
    await editor.selectWords(block, phrase);
    await expect(editor.selectionBar).toBeVisible();
  });

When(/^the reviewer selects from "([^"]*)" to "([^"]*)"$/,
  async ({ editor }, first, last) => {
    await editor.selectAcross(first, last);
    await expect(editor.selectionBar).toBeVisible();
  });

When(/^the reviewer chooses "(Edit text|Delete|Comment)" for the selection$/,
  async ({ editor }, label) => {
    await editor.choose(label);
  });

When('the reviewer clicks the marked text', async ({ editor }) => {
  await editor.cutText.click();
  await expect(editor.undoBar).toBeVisible();
});

When(/^the reviewer chooses "(Undo this edit)"$/, async ({ editor }, label) => {
  await editor.choose(label);
});

Then(/^the artifact keeps the words "([^"]*)"$/, async ({ editor }, phrase) => {
  await expect(editor.frame.locator('body')).toContainText(phrase);
  await expect(editor.cutText).toHaveCount(0);
});

Then('the line is marked as edited', async ({ editor }) => {
  await expect(editor.edited).toBeVisible();
});

Then('the cut words are struck through', async ({ editor }) => {
  await expect(editor.cutText).toBeVisible();
});

Then('the line is marked for deletion', async ({ editor }) => {
  await expect(editor.cutLine).toBeVisible();
});

// A second trigger click cannot close the popover. That click dismisses it,
// and then the trigger handler opens it again.
When('the reviewer closes the composer menu', async ({ page }) => {
  await page.keyboard.press('Escape');
  await expect(page.locator('#chatMenu:popover-open')).toHaveCount(0);
});

Then('the composer menu also offers saving into the artifact', async ({ page }) => {
  const menu = page.locator('#chatMenu');
  await expect(menu.locator('[role="menuitem"]:not([hidden])')).toHaveCount(4);
  await expect(page.locator('#chatApply')).toBeVisible();
});

Then(/^the draft chip reads "([^"]*)"$/, async ({ rail }, label) => {
  await expect(rail.drafts.locator('.state-chip').first()).toHaveText(label);
});

Then(/^the composer button reads "([^"]*)"$/, async ({ rail }, label) => {
  await expect(rail.actionLabel).toHaveText(label);
});

When('the reviewer removes the first draft', async ({ rail }) => {
  await rail.drafts.locator('.remove').first().click();
});

Then(/^the artifact reads "([^"]*)"$/, async ({ editor }, text) => {
  await expect(editor.frame.locator('p').first()).toHaveText(text);
});

Then(/^the artifact file carries "([^"]*)"$/, async ({ artifact }, text) => {
  await expect.poll(() => artifact.read()).toContain(text);
});

Then('the artifact file still reads as the agent wrote it', async ({ artifact }) => {
  expect(await artifact.read()).toBe(artifact.original);
});

Then(/^the agent receives an edit carrying "([^"]*)"$/, async ({ arev }, text) => {
  const event = await arev.awaitEvent('feedback');
  const edits = event.items.filter(item => item.kind === 'text-edit');
  expect(edits.length).toBeGreaterThan(0);
  expect(edits.flatMap(item => item.blocks.map(block => block.after)).join('\n'))
    .toContain(text);
});

Then('the delivered feedback is marked as already applied', async ({ arev }) => {
  expect(arev.lastEvent.applied).toBe(true);
});

Then('the delivered feedback is not marked as applied', async ({ arev }) => {
  expect(arev.lastEvent.applied).toBeUndefined();
});

When('the agent rewrites the artifact', async ({ artifact, editor }) => {
  await artifact.append('\n<p>The agent added this line.</p>\n');
  // A hover sent before the frame reloads lands on the old document.
  await expect(editor.frame.getByText('The agent added this line.')).toBeVisible();
});

export { FIRST, SECOND };
