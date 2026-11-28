import { test, expect, seedDocument, newConversation, ask, documentText, noOverflow } from './helpers';
import fs from 'node:fs';

test('Документ: валидация, индексация, вопрос и безопасная цитата', async ({ signedPage: page }) => {
  await page.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: 'Документы' }).click();
  await page.getByRole('button', { name: '＋ Добавить документ' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Название документа').fill('Регламент с цитатой');
  await dialog.getByLabel('Текст документа').fill('   ');
  await dialog.getByRole('button', { name: 'Добавить и обработать' }).click();
  await expect(dialog.getByRole('alert')).toBeVisible();
  await expect(dialog.getByLabel('Название документа')).toHaveValue('Регламент с цитатой');
  const source = documentText + ' <img src=x onerror="window.badHtml=true">';
  await dialog.getByLabel('Текст документа').fill(source);
  await dialog.getByRole('button', { name: 'Добавить и обработать' }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole('heading', { name: 'Регламент с цитатой' })).toBeVisible();
  await expect(page.getByText('Готов к поиску', { exact: true })).toBeVisible();
  await newConversation(page);
  await ask(page, 'Входит ли духовка в генеральную уборку?');
  await page.getByText('Источники (1)', { exact: true }).click();
  await expect(page.locator('blockquote')).toHaveText(source);
  expect(await page.evaluate(() => (window as any).badHtml)).toBeUndefined();
  await page.getByRole('link', { name: 'Открыть документ' }).click();
  await expect(page.getByRole('heading', { name: 'Регламент с цитатой' })).toBeVisible();
  await page.getByRole('button', { name: 'Повторить индексацию' }).click();
  await expect(page.getByRole('button', { name: 'Повторить индексацию' })).toBeEnabled();
  await page.getByRole('button', { name: 'Исключить из поиска' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Исключить', exact: true }).click();
  await expect(page.getByText('Исключён из поиска', { exact: true })).toBeVisible();
  await noOverflow(page);
});

test('Диалоги: память, черновики, переименование, история, аудит и экспорт', async ({ signedPage: page }) => {
  await seedDocument(page);
  const first = await newConversation(page);
  await ask(page, 'Что входит в генеральную уборку?');
  await ask(page, 'А духовка туда входит?', true);
  await expect(page.getByRole('article', { name: 'Помощник' })).toHaveCount(2);
  await page.getByLabel('Вопрос по обращению').fill('Черновик первого обращения');
  const second = await newConversation(page);
  await expect(page.getByRole('article', { name: 'Помощник' })).toHaveCount(0);
  await page.getByLabel('Вопрос по обращению').fill('Черновик другого обращения');
  await page.goto(`/#dialogs/${first}`);
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('Черновик первого обращения');
  await page.getByLabel('Вопрос по обращению').fill('Сохранить при переключении');
  await page.getByRole('link', { name: 'Новый диалог', exact: false }).last().click();
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('Черновик другого обращения');
  await page.locator(`a[href="#dialogs/${first}"]`).click();
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('Сохранить при переключении');
  await page.reload();
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('');
  await expect(page.getByRole('article', { name: 'Помощник' })).toHaveCount(2);
  await page.getByRole('button', { name: 'Переименовать', exact: true }).click();
  await page.getByRole('dialog').getByLabel('Название', { exact: true }).fill('Обращение по квартире');
  await page.getByRole('dialog').getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Обращение по квартире', exact: true })).toBeVisible();
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Экспорт JSON' }).click();
  const download = await downloaded;
  const exported = JSON.parse(fs.readFileSync((await download.path())!, 'utf8'));
  expect(exported.conversation.runs).toHaveLength(2);
  await page.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: 'История' }).click();
  await page.getByRole('link', { name: 'Открыть результат' }).first().click();
  await expect(page.getByRole('region', { name: 'Выбранный результат' })).toBeVisible();
  await page.getByRole('link', { name: 'Журнал действий' }).click();
  await expect(page.getByText('Диалог переименован', { exact: true })).toBeVisible();
  await page.goto(`/#dialogs/${second}`);
  await page.getByRole('button', { name: 'Удалить', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Отмена' }).click();
  await expect(page.getByLabel('Вопрос по обращению')).toBeVisible();
  await page.getByRole('button', { name: 'Удалить', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить диалог', exact: true }).click();
  await expect(page).toHaveURL(/#dialogs$/);
  await expect(page.locator(`a[href="#dialogs/${second}"]`)).toHaveCount(0);
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Вход в панель' })).toBeVisible();
  await page.goto(`/#dialogs/${first}`);
  await expect(page.getByRole('heading', { name: 'Вход в панель' })).toBeVisible();
});

test('Проверка ответа, снимки итога и устаревание', async ({ signedPage: page }) => {
  await seedDocument(page);
  const cid = await newConversation(page);
  await expect(page.getByRole('button', { name: 'Подвести итог', exact: true })).toBeDisabled();
  await ask(page, 'Что входит в генеральную уборку?');
  await ask(page, 'Сколько точно будет стоить генеральная уборка?');
  await expect(page.getByText('Нужна проверка', { exact: true }).first()).toBeVisible();
  await page.getByLabel('Вопрос по обращению').fill('Черновик не должен исчезнуть при прокрутке и итоге');
  await page.getByRole('button', { name: '↑ К началу диалога', exact: true }).click();
  await expect(page.locator('#thread-start h2')).toBeInViewport();
  await page.getByRole('button', { name: '↓ К концу диалога', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Подвести итог', exact: true })).toBeInViewport();
  await expect(page.getByLabel('Вопрос по обращению')).toBeInViewport();
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('Черновик не должен исчезнуть при прокрутке и итоге');
  await page.getByRole('button', { name: 'Подвести итог', exact: true }).click();
  await expect(page.getByRole('article', { name: 'Итог беседы' })).toHaveCount(1);
  await expect(page.getByRole('article', { name: 'Итог беседы' }).locator('.message-label')).toBeInViewport();
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('Черновик не должен исчезнуть при прокрутке и итоге');
  await expect(page.getByRole('article', { name: 'Итог беседы' })).toContainText('ожидает проверки');
  await page.getByRole('button', { name: 'Отметить проверенным', exact: true }).click();
  await expect(page.getByText('Требовало проверки', { exact: true })).toBeVisible();
  await expect(page.getByRole('article', { name: 'Итог беседы' }).getByText('Устарел', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Подвести итог', exact: true }).click();
  await expect(page.getByRole('article', { name: 'Итог беседы' })).toHaveCount(2);
  await expect(page.getByRole('article', { name: 'Итог беседы' }).last().locator('.message-label')).toBeInViewport();
  await expect(page.getByRole('article', { name: 'Итог беседы' }).last()).toContainText('результат не записан');
  if (process.env.UI_CAPTURE === '1') {
    await page.screenshot({ path: 'output/stage7-summary-desktop.png', animations: 'disabled' });
  }
  await page.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: 'История' }).click();
  await page.getByLabel('Требуют проверки', { exact: true }).check();
  await expect(page.getByRole('heading', { name: 'Нет ответов в очереди проверки' })).toBeVisible();
  await page.goto(`/#dialogs/${cid}`);
  await ask(page, 'Как подготовить духовку?');
  await expect(page.getByText('Устарел', { exact: true })).toHaveCount(2);
});

test('Поздний итог не переключает пользователя из другого диалога', async ({ signedPage: page }) => {
  await seedDocument(page);
  const first = await newConversation(page);
  await ask(page, 'Что входит в генеральную уборку?');
  const second = await newConversation(page);
  await page.goto(`/#dialogs/${first}`);
  let release: () => void;
  await page.route(`**/conversations/${first}/summary`, async (route) => {
    const response = await route.fetch();
    await new Promise<void>((resolve) => { release = resolve; });
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Подвести итог', exact: true }).click();
  await expect.poll(() => typeof release).toBe('function');
  await expect(page.locator('#view')).toHaveAttribute('aria-busy', 'false');
  await page.locator(`a[href="#dialogs/${second}"]`).click();
  await page.getByLabel('Вопрос по обращению').fill('Черновик другого диалога');
  const finished = page.waitForResponse((r) => r.url().endsWith(`/conversations/${first}/summary`));
  release!();
  await finished;
  await expect(page).toHaveURL(new RegExp(`#dialogs/${second}$`));
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('Черновик другого диалога');
  await expect(page.getByRole('article', { name: 'Итог беседы' })).toHaveCount(0);
  await page.locator(`a[href="#dialogs/${first}"]`).click();
  await expect(page.getByRole('article', { name: 'Итог беседы' })).toHaveCount(1);
});

test('Ошибка и лимит: ввод сохраняется, повторная отправка блокируется', async ({ signedPage: page }) => {
  // Только этот сценарий подменяет ответы в браузере; контракт сервера отдельно проверен pytest.
  await seedDocument(page);
  const cid = await newConversation(page);
  let release: () => void;
  let mode = 'error';
  let sent = 0;
  await page.route('**/kb/ask', async (route) => {
    sent++;
    if (mode === 'error') await new Promise<void>((resolve) => { release = resolve; });
    await route.fulfill({ status: mode === 'error' ? 503 : 409, json: { error: {
      code: mode === 'error' ? 'provider_timeout' : 'dialog_limit_reached',
      message: mode === 'error' ? 'Сервис ответа не завершил обработку вовремя' : 'Достигнут предел диалога; начните новый',
    } } });
  });
  await page.getByLabel('Вопрос по обращению').fill('Не потерять этот вопрос');
  await page.getByRole('button', { name: 'Отправить вопрос' }).click();
  await expect.poll(() => typeof release).toBe('function');
  await expect(page.getByRole('button', { name: 'Готовим ответ…' })).toBeDisabled();
  await expect(page.getByLabel('Вопрос по обращению')).toBeDisabled();
  release!();
  await expect(page.getByRole('alert')).toContainText('не завершил');
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('Не потерять этот вопрос');
  expect(sent).toBe(1);
  mode = 'limit';
  await page.route(`**/conversations/${cid}`, async (route) => {
    const response = await route.fetch(); const data = await response.json();
    data.budget.can_ask = false;
    await route.fulfill({ response, json: data });
  });
  await page.getByRole('button', { name: 'Отправить вопрос' }).click();
  await expect(page.getByRole('alert')).toContainText('предел');
  await expect(page.getByLabel('Вопрос по обращению')).toHaveValue('Не потерять этот вопрос');
  await expect(page.getByRole('button', { name: 'Отправить вопрос' })).toBeDisabled();
  expect(sent).toBe(2);
});
