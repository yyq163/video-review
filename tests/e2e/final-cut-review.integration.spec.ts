import { access } from 'node:fs/promises';
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const videoPath = process.env.FCR_E2E_VIDEO_PATH;
if (!videoPath) {
  throw new Error('FCR_E2E_VIDEO_PATH is required for the real-stack E2E profile');
}
const apiBaseUrl = process.env.FCR_E2E_API_BASE_URL;
if (!apiBaseUrl) {
  throw new Error('FCR_E2E_API_BASE_URL is required for the real-stack E2E profile');
}
if (process.env.FCR_E2E_DISPOSABLE_DATABASE !== '1') {
  throw new Error('FCR_E2E_DISPOSABLE_DATABASE=1 is required; real-stack E2E must not use the application database');
}

let cleanupProjectNames: string[] = [];

test.afterEach(async ({ page }) => {
  if (cleanupProjectNames.length === 0) return;
  await page.goto('/review/projects');
  for (const projectName of cleanupProjectNames) {
    const projectCard = page.locator('.fj-review-project-card').filter({ hasText: projectName }).first();
    if ((await projectCard.count()) === 0) continue;
    const restore = projectCard.getByRole('button', { name: '恢复' });
    if (await restore.isVisible().catch(() => false)) await restore.click();
    const deleteProject = projectCard.getByRole('button', { name: '删除项目' });
    if (await deleteProject.isVisible().catch(() => false)) {
      page.once('dialog', (dialog) => dialog.accept());
      await deleteProject.click();
      await expect(projectCard).toHaveCount(0);
    }
  }
  cleanupProjectNames = [];
});

async function assertPostgresRuntime(request: APIRequestContext) {
  const response = await request.get(`${apiBaseUrl.replace(/\/+$/, '')}/runtimez`);
  expect(response.ok()).toBe(true);
  const body = (await response.json()) as {
    status?: string;
    database?: string;
    database_engine?: string;
    alembic_current?: string;
    alembic_head?: string;
  };
  expect(body).toMatchObject({
    status: 'ready',
    database: 'ok',
    database_engine: 'postgresql',
  });
  expect(body.alembic_current).toBeTruthy();
  expect(body.alembic_current).toBe(body.alembic_head);
}

async function appendVersion(page: Page, label: 'V2' | 'V3') {
  const panel = page.getByTestId('append-version-panel');
  await panel.getByTestId('append-version-file').setInputFiles(videoPath);
  await panel.getByRole('button', { name: `追加 ${label}` }).click();
  await expect(page.getByTestId('upload-progress')).toBeVisible();
  await expect(page.getByTestId(`version-${label}`)).toBeVisible();
}

test('real proxy, backend, and PostgreSQL persist one V1/V2/V3 chain', async ({ page, request }) => {
  await access(videoPath);
  await assertPostgresRuntime(request);
  const expectedApiPrefix = apiBaseUrl.replace(/\/+$/, '');
  const businessRequestUrls: string[] = [];
  page.on('request', (browserRequest) => {
    const requestUrl = browserRequest.url();
    const pathname = new URL(requestUrl).pathname;
    if (pathname.includes('/api/v1/final-cut-review/') || pathname.includes('/api/v1/files/')) {
      businessRequestUrls.push(requestUrl);
    }
  });
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  const projectName = `真实栈 E2E ${runId}`;
  cleanupProjectNames = [projectName];

  await page.goto('/edit/projects');
  await expect(page.locator('#root')).toHaveAttribute('data-review-runtime', 'http');
  await page.getByRole('button', { name: /新建项目/ }).click();
  await page.getByLabel('项目名称').fill(projectName);
  await page.getByLabel('项目编码').fill(`REAL-${runId}`);
  await page.getByLabel('项目说明').fill('真实前端、后端与 PostgreSQL 自动化联调。');
  await page.getByRole('button', { name: '创建项目' }).click();
  await expect(page.getByRole('heading', { name: new RegExp(projectName) })).toBeVisible();

  const updatedProjectName = `${projectName} 已编辑`;
  cleanupProjectNames = [updatedProjectName, projectName];
  await page.getByRole('button', { name: '编辑项目资料' }).click();
  await expect(page.getByLabel('项目编号')).toHaveAttribute('readonly');
  await page.getByLabel('项目名称').fill(updatedProjectName);
  await page.getByLabel('项目说明').fill('真实 PostgreSQL 项目元数据已编辑。');
  await page.getByRole('button', { name: '保存项目资料' }).click();
  await expect(page.getByText('项目资料已更新。')).toBeVisible();
  await expect(page.getByRole('heading', { name: new RegExp(updatedProjectName) })).toBeVisible();

  const createPanel = page.getByTestId('create-item-upload');
  await createPanel.getByTestId('create-item-file').setInputFiles(videoPath);
  await createPanel.getByRole('button', { name: '上传 V1' }).click();
  await expect(page.getByTestId('upload-progress')).toBeVisible();
  await expect(createPanel.getByRole('button', { name: '上传 V1' })).toBeVisible();

  const table = page.getByTestId('review-item-table');
  await table.getByRole('button', { name: /编辑成片元数据/ }).first().click();
  const metadataDialog = page.getByRole('dialog', { name: /编辑成片元数据/ });
  await expect(metadataDialog.getByLabel(/成片编号/)).toHaveAttribute('readonly');
  await metadataDialog.getByLabel('成片标题').fill('真实栈已编辑成片');
  await metadataDialog.getByLabel('集数').fill('38');
  await metadataDialog.getByRole('button', { name: '保存成片元数据' }).click();
  await expect(page.getByText('成片「真实栈已编辑成片」元数据已更新。')).toBeVisible();

  const updatedItem = table.locator('article').filter({ hasText: '真实栈已编辑成片' });
  await expect(updatedItem.getByText('真实栈已编辑成片', { exact: true })).toBeVisible();
  await expect(updatedItem).toContainText('第 38 集');
  await table.getByRole('link', { name: /查看与追加/ }).click();
  await expect(page.getByTestId('version-V1')).toBeVisible();

  await appendVersion(page, 'V2');
  await appendVersion(page, 'V3');
  await expect(page.getByTestId('version-V1')).toBeVisible();
  await expect(page.getByTestId('version-V2')).toBeVisible();
  await expect(page.getByTestId('version-V3')).toBeVisible();

  await page.reload();
  await assertPostgresRuntime(request);
  await expect(page.locator('#root')).toHaveAttribute('data-review-runtime', 'http');
  await expect(page.getByTestId('version-V3')).toBeVisible();
  await expect(page.getByTestId('version-V2')).toBeVisible();
  await expect(page.getByTestId('version-V1')).toBeVisible();

  await page.getByRole('link', { name: '返回项目' }).click();
  const disposableTitle = `审核前删除 ${runId}`;
  const disposableUpload = page.getByTestId('create-item-upload');
  await disposableUpload.getByLabel('成片标题').fill(disposableTitle);
  await disposableUpload.getByLabel('集数').fill('39');
  await disposableUpload.getByTestId('create-item-file').setInputFiles(videoPath);
  await disposableUpload.getByRole('button', { name: '上传 V1' }).click();
  const disposableItem = page.getByTestId('review-item-table').locator('article').filter({ hasText: disposableTitle });
  await expect(disposableItem).toBeVisible();
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('永久删除');
    await dialog.accept();
  });
  await disposableItem.getByRole('button', { name: new RegExp(`删除分集 ${disposableTitle}`) }).click();
  await expect(disposableItem).toHaveCount(0);

  const primaryItem = page.getByTestId('review-item-table').locator('article').filter({ hasText: '真实栈已编辑成片' });
  await primaryItem.getByRole('link', { name: /查看与追加/ }).click();

  await page.goto(page.url().replace('/edit/', '/review/'));
  await page.getByRole('button', { name: '开始审阅' }).click();
  await page.getByPlaceholder('针对当前版本输入意见').fill('真实栈初始意见');
  await page.getByRole('button', { name: '提交意见' }).click();
  const issueCard = page.locator('.fj-review-issue-card').filter({ hasText: '真实栈初始意见' });
  await expect(issueCard).toBeVisible();
  await issueCard.getByRole('button', { name: '编辑意见' }).click();
  await issueCard.getByLabel('编辑意见正文').fill('真实栈第二修订');
  await issueCard.getByRole('button', { name: '保存为新修订' }).click();
  await expect(issueCard).toContainText('R2');
  await issueCard.getByText('修订历史（2）').click();
  await expect(issueCard.getByText('历史修订只读')).toBeVisible();
  await expect(issueCard.getByText('真实栈初始意见')).toBeVisible();

  await page.reload();
  const reloadedIssueCard = page.locator('.fj-review-issue-card').filter({ hasText: '真实栈第二修订' });
  await expect(reloadedIssueCard).toContainText('R2');
  await reloadedIssueCard.getByText('修订历史（2）').click();
  await expect(reloadedIssueCard.getByText('真实栈初始意见')).toBeVisible();
  await reloadedIssueCard.getByRole('button', { name: '解决当前版本意见' }).click();
  await expect(page.getByText('当前版本可定稿')).toBeVisible();
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('确认将当前版本定稿');
    await dialog.accept();
  });
  await page.getByTestId('finalize-current').click();
  await expect(page.getByText('当前版本已精确定稿冻结。')).toBeVisible();
  await expect(page.getByTestId('issue-form')).toHaveCount(0);

  await page.goto('/review/projects');
  const projectCard = page.locator('.fj-review-project-card').filter({ hasText: updatedProjectName });
  await expect(projectCard).toBeVisible();
  await projectCard.getByRole('button', { name: '归档' }).click();
  await expect(projectCard).toContainText('已归档');
  await expect(projectCard.getByRole('button', { name: '删除项目' })).toHaveCount(0);
  await projectCard.getByRole('button', { name: '恢复' }).click();
  await expect(projectCard).toContainText('进行中');
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('确认删除项目');
    await dialog.accept();
  });
  await projectCard.getByRole('button', { name: '删除项目' }).click();
  await expect(projectCard).toHaveCount(0);
  expect(businessRequestUrls.length).toBeGreaterThan(0);
  expect(businessRequestUrls.every((requestUrl) => requestUrl.startsWith(`${expectedApiPrefix}/api/v1/`))).toBe(true);
});
