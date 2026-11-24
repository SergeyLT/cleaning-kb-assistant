import { test, expect, seedDocument, newConversation, ask, noOverflow } from './helpers';

test('Телефон: список, беседа, источник и основные действия', async ({ signedPage: page }) => {
  await seedDocument(page);
  const documentsNav = page.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: 'Документы' });
  expect(await documentsNav.evaluate((link) => link.scrollWidth <= link.clientWidth)).toBeTruthy();
  const cid = await newConversation(page);
  await ask(page, 'Сколько точно будет стоить генеральная уборка?');
  await page.getByText('Источники (1)', { exact: true }).click();
  await page.getByRole('link', { name: 'Открыть документ' }).click();
  await expect(page.getByRole('heading', { name: /Правила уборки/ })).toBeVisible();
  await noOverflow(page);
  await page.getByRole('link', { name: '← Назад', exact: true }).click();
  await page.getByRole('button', { name: 'Отметить проверенным' }).click();
  await expect(page.getByText('Требовало проверки', { exact: true })).toBeVisible();
  const summary = page.getByRole('button', { name: 'Подвести итог', exact: true });
  await summary.scrollIntoViewIfNeeded();
  await expect(summary).toBeEnabled();
  await summary.click();
  await expect(page.getByRole('article', { name: 'Итог беседы' })).toBeVisible();
  await page.getByRole('link', { name: '← Все диалоги' }).click();
  await expect(page.getByRole('complementary', { name: 'Сохранённые диалоги' })).toBeVisible();
  await page.locator(`a[href="#dialogs/${cid}"]`).click();
  await expect(page.getByRole('link', { name: '← Все диалоги' })).toBeVisible();
  if (process.env.UI_CAPTURE === '1') {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: 'output/stage4-mobile.png', animations: 'disabled' });
  }
  await page.getByRole('button', { name: 'Удалить', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Отмена' })).toBeVisible();
  await page.keyboard.press('Escape');
  await noOverflow(page);
});
