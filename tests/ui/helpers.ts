import { test as base, expect, Page } from '@playwright/test';

const headers = { 'X-API-Key': 'ui-e2e-only-key-0123456789-abcdef' };
export const documentText = 'Духовка — отдельная опция генеральной уборки. Перед чисткой её выключают и остужают.';
export const test = base.extend<{ signedPage: Page }>({
  signedPage: async ({ page, request }, use) => {
    // Случайное подключение к рабочему приложению запрещено: проверяем явную версию test-factory.
    const status = await request.get('/api/status', { headers });
    expect((await status.json()).version).toBe('ui-e2e');
    await page.goto('/');
    await page.getByLabel('Логин', { exact: true }).fill('manager');
    await page.getByLabel('Пароль', { exact: true }).fill('ui-test-password-only');
    await page.getByRole('button', { name: 'Войти', exact: true }).click();
    await expect(page.getByRole('navigation', { name: 'Разделы' })).toBeVisible();
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    await use(page);
    expect(pageErrors).toEqual([]);
    const conversations = await (await request.get('/conversations', { headers })).json();
    for (const item of conversations) await request.delete(`/conversations/${item.id}?confirm=true`, { headers });
    const documents = await (await request.get('/kb/documents', { headers })).json();
    for (const item of documents.filter((d: { is_active: number }) => d.is_active)) await request.post(`/kb/documents/${item.id}/deactivate`, { headers });
  },
});
export { expect };

export async function seedDocument(page: Page, title = 'Правила уборки') {
  // Подготовка фикстуры через настоящее API. Само добавление через форму проверяется отдельно.
  const response = await page.request.post('/kb/documents', { headers, data: { title: `${title} ${Date.now()}`, text: documentText } });
  expect(response.ok()).toBeTruthy();
  return response.json();
}
export async function newConversation(page: Page) {
  await page.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: 'Диалоги' }).click();
  await page.getByRole('button', { name: '＋ Новый диалог', exact: true }).click();
  await expect(page.getByLabel('Вопрос по обращению')).toBeVisible();
  return new URL(page.url()).hash.split('/')[1].split('?')[0];
}
export async function ask(page: Page, text: string, keyboard = false) {
  await page.getByLabel('Вопрос по обращению').fill(text);
  const response = page.waitForResponse((r) => r.url().endsWith('/kb/ask') && r.request().method() === 'POST');
  if (keyboard) await page.getByLabel('Вопрос по обращению').press('Control+Enter');
  else await page.getByRole('button', { name: 'Отправить вопрос', exact: true }).click();
  expect((await response).ok()).toBeTruthy();
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('');
  await expect(page.locator('#view')).toHaveAttribute('aria-busy', 'false');
}
export async function noOverflow(page: Page) {
  const geometry = await page.evaluate(() => {
    const viewport = document.documentElement.clientWidth;
    return {
      viewport,
      width: document.documentElement.scrollWidth,
      overflowing: Array.from(document.body.querySelectorAll('*')).flatMap((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width && (rect.right > viewport || rect.left < 0)
          ? [{ tag: element.tagName, class: element.className, left: rect.left, right: rect.right }]
          : [];
      }).slice(0, 15),
    };
  });
  expect(geometry.width, `Горизонтальное переполнение: ${JSON.stringify(geometry)}`).toBeLessThanOrEqual(geometry.viewport);
}
