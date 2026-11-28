import { test, expect, seedDocument, newConversation, ask, noOverflow } from './helpers';

test('Компоновка ключевых экранов и длинного текста в трёх размерах', async ({ signedPage: page }) => {
  await seedDocument(page, 'ОченьДлинноеНазвание'.repeat(8));
  const cid = await newConversation(page);
  await ask(page, 'ВопросБезПробелов'.repeat(90));
  for (const size of [{ width: 375, height: 812 }, { width: 768, height: 1024 }, { width: 1280, height: 800 }]) {
    await page.setViewportSize(size);
    for (const hash of [`dialogs/${cid}`, 'documents', 'history', 'settings']) {
      await page.goto('/#' + hash);
      await expect(page.locator('#view')).toHaveAttribute('aria-busy', 'false');
      await noOverflow(page);
      const navigation = page.getByRole('navigation', { name: 'Разделы' });
      await expect(navigation).toBeVisible();
      if (hash.startsWith('dialogs/')) {
        await expect(page.getByLabel('Вопрос по обращению')).toBeEditable();
        const submit = page.getByRole('button', { name: 'Отправить вопрос' });
        await submit.scrollIntoViewIfNeeded();
        const box = await submit.boundingBox();
        expect(box!.height).toBeGreaterThanOrEqual(44);
        expect(box!.width).toBeGreaterThanOrEqual(44);
        expect(await page.getByLabel('Вопрос по обращению').evaluate((input) => parseFloat(getComputedStyle(input).fontSize))).toBeGreaterThanOrEqual(16);
      }
    }
  }
});
