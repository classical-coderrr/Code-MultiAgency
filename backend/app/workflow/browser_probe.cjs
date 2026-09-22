// Deterministic browser gate. Only visits the isolated validation server.
const fs = require('fs');
const path = require('path');
const { chromium } = require(process.env.ARTIFACT_BROWSER_MODULE || path.resolve(__dirname, '../../../frontend/node_modules/playwright'));

(async () => {
  const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
  const url = new URL(spec.url);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname)) throw new Error('Only loopback validation URLs are permitted');
  let browser;
  let page;
  const errors = [];
  const consoleMessages = [];
  const networkEvents = [];
  const crudStates = [];
  const formValues = {};
  const screenshotPaths = [];
  let domSnapshot = '';
  let visibleTextChars = 0;
  let uiCrud = false;

  async function capture(label) {
    if (!page) return;
    const bodyText = (await page.locator('body').innerText().catch(() => '')).trim();
    visibleTextChars = bodyText.length;
    domSnapshot = (await page.locator('body').evaluate(element => element.outerHTML).catch(() => '')).slice(0, 12000);
    const values = await page.locator('input,select,textarea').evaluateAll(elements => Object.fromEntries(elements.map((element, index) => [
      element.getAttribute('data-testid') || element.getAttribute('name') || `field-${index}`,
      element.type === 'checkbox' ? Boolean(element.checked) : String(element.value || '')
    ]))).catch(() => ({}));
    Object.assign(formValues, values);
    crudStates.push({label, bodyText: bodyText.slice(0, 3000), formValues: values});
    if (spec.evidenceDir) {
      fs.mkdirSync(spec.evidenceDir, {recursive: true});
      const screenshotPath = path.join(spec.evidenceDir, `${spec.evidenceId || 'browser'}-${label}.png`);
      await page.screenshot({path: screenshotPath, fullPage: true});
      screenshotPaths.push(screenshotPath);
    }
  }

  try {
    browser = await chromium.launch({headless: true});
    page = await browser.newPage();
    page.on('console', message => consoleMessages.push({type: message.type(), text: message.text().slice(0, 1000)}));
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => {
      const required = new URL(response.url()).origin === url.origin || ['script', 'stylesheet', 'image'].includes(response.request().resourceType());
      if (networkEvents.length < 100) networkEvents.push({method: response.request().method(), url: response.url(), status: response.status(), resourceType: response.request().resourceType()});
      if (required && response.status() >= 400 && !response.url().endsWith('/favicon.ico')) errors.push(`${new URL(response.url()).pathname}=HTTP ${response.status()}`);
    });
    page.on('requestfailed', request => {
      if (networkEvents.length < 100) networkEvents.push({method: request.method(), url: request.url(), status: 0, error: request.failure()?.errorText || 'request failed'});
      if (request.failure()?.errorText !== 'net::ERR_ABORTED' && !request.url().endsWith('/favicon.ico')) errors.push(`${request.url()}: ${request.failure()?.errorText}`);
    });
    page.on('dialog', dialog => dialog.accept());
    page.setDefaultTimeout(15000);
    await page.goto(spec.url, {waitUntil: 'networkidle', timeout: 30000});
    const body = (await page.locator('body').innerText()).trim();
    if (body.length < 20) throw new Error('页面渲染后正文过少或空白');
    await capture('before');
    const api = (spec.contract.api_contract || [])[0];
    if (spec.contract.crud_required && api && spec.contract.backend_stack !== 'none') {
      // Exercise actual UI operations rather than calling the API in Node.
      const payload = {...api.payload};
      const marker = Object.keys(payload).find(key => typeof payload[key] === 'string' && !/email|phone|date/i.test(key));
      if (!marker) throw new Error('UI CRUD 合同缺少可修改的文本字段');
      payload[marker] = ('A' + Math.random().toString(36).slice(2)).slice(0, Math.max(2, String(payload[marker]).length));
      async function fill(values) {
        for (const [key, value] of Object.entries(values)) {
          const field = page.getByTestId(`crud-field-${key}`);
          const tag = await field.evaluate(element => element.tagName.toLowerCase());
          if (tag === 'select') await field.selectOption(String(value));
          else if (typeof value === 'boolean') await field.setChecked(value);
          else await field.fill(String(value));
        }
      }
      await page.getByTestId('crud-add').click();
      await fill(payload);
      await page.getByTestId('crud-save').click();
      let row = page.getByTestId('crud-row').filter({hasText: payload[marker]});
      await row.first().waitFor({state:'visible'});
      await capture('after-create');
      await row.first().getByTestId('crud-edit').click();
      const updated = {...payload, [marker]: 'Z' + payload[marker].slice(1)};
      await fill(updated);
      await page.getByTestId('crud-save').click();
      row = page.getByTestId('crud-row').filter({hasText: updated[marker]});
      await row.first().waitFor({state:'visible'});
      await capture('after-update');
      await row.first().getByTestId('crud-delete').click();
      await row.first().waitFor({state:'hidden'});
      await capture('after-delete');
      uiCrud = true;
    }
    if (spec.contract.crud_required && spec.contract.backend_stack === 'none') {
      // A browser-only CRUD application has no HTTP API or H2 evidence.
      // Verify its actual controls and persistence across reloads instead.
      const entity = (spec.contract.entities || [])[0] || {};
      const fields = Object.entries(entity.fields || {});
      const editable = fields.find(([name, details]) =>
        name !== 'id' && !details?.generated &&
        (!details?.json_type || details.json_type === 'string') &&
        !/email|phone|date/i.test(name)
      );
      if (!editable) throw new Error('本地 CRUD 合同缺少可测试的文本字段');
      const field = page.getByTestId(`crud-field-${editable[0]}`);
      const first = `A${Math.random().toString(36).slice(2, 9)}`;
      const changed = `Z${first.slice(1)}`;
      await page.getByTestId('crud-add').click();
      await field.fill(first);
      await page.getByTestId('crud-save').click();
      let row = page.getByTestId('crud-row').filter({hasText: first});
      await row.first().waitFor({state: 'visible'});
      await capture('after-create');
      await page.reload({waitUntil: 'networkidle'});
      row = page.getByTestId('crud-row').filter({hasText: first});
      await row.first().waitFor({state: 'visible'});
      await row.first().getByTestId('crud-edit').click();
      await page.getByTestId(`crud-field-${editable[0]}`).fill(changed);
      await page.getByTestId('crud-save').click();
      row = page.getByTestId('crud-row').filter({hasText: changed});
      await row.first().waitFor({state: 'visible'});
      await capture('after-update');
      await page.reload({waitUntil: 'networkidle'});
      row = page.getByTestId('crud-row').filter({hasText: changed});
      await row.first().waitFor({state: 'visible'});
      await row.first().getByTestId('crud-delete').click();
      await row.first().waitFor({state: 'hidden'});
      await capture('after-delete');
      await page.reload({waitUntil: 'networkidle'});
      if (await page.getByTestId('crud-row').filter({hasText: changed}).count())
        throw new Error('删除后刷新页面，记录仍然存在');
      uiCrud = true;
    }
    if (errors.length) throw new Error(errors.join('; ').slice(0,3000));
    console.log(JSON.stringify({
      status:'passed', visibleTextChars, uiCrud, path:url.pathname,
      screenshotPath:screenshotPaths[screenshotPaths.length - 1] || '', screenshotPaths,
      domSnapshot, consoleMessages:consoleMessages.slice(-50),
      networkEvents:networkEvents.slice(-100), formValues,
      crudStates:crudStates.slice(-8)
    }));
  } catch (error) {
    try { await capture('failure'); } catch (_) {}
    console.log(JSON.stringify({
      status:'failed', message:error.message, visibleTextChars, uiCrud,
      screenshotPath:screenshotPaths[screenshotPaths.length - 1] || '', screenshotPaths,
      domSnapshot, consoleMessages:consoleMessages.slice(-50),
      networkEvents:networkEvents.slice(-100), formValues,
      crudStates:crudStates.slice(-8)
    }));
    process.exitCode=1;
  } finally {
    if (browser) await browser.close();
  }
})();
