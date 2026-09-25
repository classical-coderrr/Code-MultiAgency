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
  let phase = 'open-page';

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

  async function waitForEditField(fieldName, expectedValue, panelTestId = '') {
    // Editing may fetch the record asynchronously. Filling before that GET
    // finishes lets the response overwrite the probe's new value.
    await page.waitForFunction(({testId, expected, panelId}) => {
      const roots = panelId ? [...document.querySelectorAll('[data-testid]')]
        .filter(element => element.getAttribute('data-testid') === panelId) : [document];
      const field = roots.map(root => [...root.querySelectorAll('[data-testid]')]
        .find(element => element.getAttribute('data-testid') === testId)).find(Boolean);
      return field && String(field.value) === expected;
    }, {testId: `crud-field-${fieldName}`, expected: String(expectedValue), panelId: panelTestId}, {timeout: 15000});
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
    phase = 'page-render';
    const body = (await page.locator('body').innerText()).trim();
    if (body.length < 20) throw new Error('页面渲染后正文过少或空白');
    await capture('before');
    const crudApis = (spec.contract.api_contract || []).filter(api => api && api.payload && (api.methods || []).includes('POST'));
    if (spec.contract.crud_required && crudApis.length && spec.contract.backend_stack !== 'none') {
      // Exercise actual UI operations rather than calling the API in Node.
      // Related-record API calls below only prepare valid test data. Every
      // entity's own create/update/delete still has to pass through the UI.
      const relatedFixtures = new Map();
      for (const api of crudApis) {
      const multiple = crudApis.length > 1;
      const entityId = String(api.entity_id || '');
      if (multiple && !entityId) throw new Error('多实体 UI CRUD 合同缺少 entity_id');
      const panelTestId = multiple ? `crud-panel-${entityId}` : '';
      // A generated view may also contain a decorative child with the same
      // marker. Select the visible panel that owns the actual CRUD controls;
      // the following operations still have to pass against that panel.
      const controls = multiple
        ? page.getByTestId(panelTestId).filter({has: page.getByTestId('crud-add')}).first()
        : page;
      if (multiple && !await controls.isVisible().catch(() => false)) {
        phase = `${entityId}:open-view`;
        const switches = page.locator('button[aria-controls], button[role="tab"], [role="tab"]');
        const count = Math.min(await switches.count(), 12);
        for (let index = 0; index < count; index++) {
          const toggle = switches.nth(index);
          if (!await toggle.isVisible().catch(() => false) || !await toggle.isEnabled().catch(() => false)) continue;
          await toggle.click();
          if (await controls.isVisible().catch(() => false)) break;
        }
      }
      phase = multiple ? `${entityId}:panel-visible` : 'panel-visible';
      if (multiple) await controls.waitFor({state: 'visible'});
      const label = suffix => multiple ? `${entityId}-${suffix}` : suffix;
      const stage = suffix => multiple ? `${entityId}:${suffix}` : suffix;
      const payload = {...api.payload};
      for (const field of Object.keys(payload)) {
        const relation = /^(.+)Id$/.exec(field);
        if (!relation || field === (api.identity_field || 'id')) continue;
        const parent = crudApis.find(candidate =>
          String(candidate.entity_id || '').toLowerCase() === relation[1].toLowerCase());
        if (!parent) continue;
        let fixture = relatedFixtures.get(parent.entity_id);
        if (!fixture) {
          const parentPayload = {...parent.payload};
          if (typeof parentPayload.stock === 'number' && typeof payload.quantity === 'number')
            parentPayload.stock = Math.max(parentPayload.stock, payload.quantity + 1);
          phase = stage(`fixture-${parent.entity_id}`);
          const response = await page.request.post(new URL(parent.path, spec.url).toString(), {data: parentPayload});
          if (!response.ok()) throw new Error(`关联测试记录 ${parent.entity_id} 创建失败：HTTP ${response.status()}`);
          const record = await response.json();
          const id = record?.[parent.record_id_field || 'id'];
          if (id === undefined || id === null) throw new Error(`关联测试记录 ${parent.entity_id} 缺少 ID`);
          fixture = {id, path: parent.path};
          relatedFixtures.set(parent.entity_id, fixture);
        }
        payload[field] = fixture.id;
      }
      const marker = Object.keys(payload).find(key => typeof payload[key] === 'string' && !/email|phone|date/i.test(key));
      if (!marker) throw new Error('UI CRUD 合同缺少可修改的文本字段');
      payload[marker] = ('A' + Math.random().toString(36).slice(2)).slice(0, Math.max(2, String(payload[marker]).length));
      async function fill(values) {
        for (const [key, value] of Object.entries(values)) {
          const field = controls.getByTestId(`crud-field-${key}`);
          const tag = await field.evaluate(element => element.tagName.toLowerCase());
          if (tag === 'select') await field.selectOption(String(value));
          else if (typeof value === 'boolean') await field.setChecked(value);
          else await field.fill(String(value));
        }
      }
      phase = stage('create-open-form');
      await controls.getByTestId('crud-add').click();
      phase = stage('create-fill-form');
      await fill(payload);
      phase = stage('create-save');
      await controls.getByTestId('crud-save').click();
      phase = stage('create-visible-row');
      let row = controls.getByTestId('crud-row').filter({hasText: payload[marker]});
      await row.first().waitFor({state:'visible'});
      await capture(label('after-create'));
      phase = stage('update-open-form');
      await row.first().getByTestId('crud-edit').click();
      await waitForEditField(marker, payload[marker], panelTestId);
      const updated = {...payload, [marker]: 'Z' + payload[marker].slice(1)};
      phase = stage('update-fill-form');
      await fill(updated);
      phase = stage('update-save');
      await controls.getByTestId('crud-save').click();
      phase = stage('update-visible-row');
      row = controls.getByTestId('crud-row').filter({hasText: updated[marker]});
      await row.first().waitFor({state:'visible'});
      await capture(label('after-update'));
      phase = stage('delete-row');
      await row.first().getByTestId('crud-delete').click();
      await row.first().waitFor({state:'hidden'});
      await capture(label('after-delete'));
      }
      for (const fixture of [...relatedFixtures.values()].reverse()) {
        phase = 'related-fixture-cleanup';
        const response = await page.request.delete(
          new URL(`${fixture.path}/${fixture.id}`, spec.url).toString());
        if (!response.ok()) throw new Error(`关联测试记录清理失败：HTTP ${response.status()}`);
      }
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
      phase = 'local-create-open-form';
      await page.getByTestId('crud-add').click();
      phase = 'local-create-fill-form';
      await field.fill(first);
      phase = 'local-create-save';
      await page.getByTestId('crud-save').click();
      phase = 'local-create-visible-row';
      let row = page.getByTestId('crud-row').filter({hasText: first});
      await row.first().waitFor({state: 'visible'});
      await capture('after-create');
      await page.reload({waitUntil: 'networkidle'});
      row = page.getByTestId('crud-row').filter({hasText: first});
      await row.first().waitFor({state: 'visible'});
      phase = 'local-update-open-form';
      await row.first().getByTestId('crud-edit').click();
      await waitForEditField(editable[0], first);
      phase = 'local-update-fill-form';
      await page.getByTestId(`crud-field-${editable[0]}`).fill(changed);
      phase = 'local-update-save';
      await page.getByTestId('crud-save').click();
      phase = 'local-update-visible-row';
      row = page.getByTestId('crud-row').filter({hasText: changed});
      await row.first().waitFor({state: 'visible'});
      await capture('after-update');
      await page.reload({waitUntil: 'networkidle'});
      row = page.getByTestId('crud-row').filter({hasText: changed});
      await row.first().waitFor({state: 'visible'});
      phase = 'local-delete-row';
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
      status:'passed', phase, visibleTextChars, uiCrud, path:url.pathname,
      screenshotPath:screenshotPaths[screenshotPaths.length - 1] || '', screenshotPaths,
      domSnapshot, consoleMessages:consoleMessages.slice(-50),
      networkEvents:networkEvents.slice(-100), formValues,
      crudStates:crudStates.slice(-8)
    }));
  } catch (error) {
    try { await capture('failure'); } catch (_) {}
    console.log(JSON.stringify({
      status:'failed', phase, message:error.message, visibleTextChars, uiCrud,
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
