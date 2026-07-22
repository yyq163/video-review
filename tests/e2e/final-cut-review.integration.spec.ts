import { access, realpath, stat } from 'node:fs/promises';
import { isAbsolute, relative, resolve, sep } from 'node:path';
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const testVideoRoot = resolve('/Volumes/App_Dev/审阅平台/test-video');
const videoPaths = [
  resolve(process.env.FCR_E2E_VIDEO_PATH ?? `${testVideoRoot}/01.mp4`),
  resolve(process.env.FCR_E2E_VIDEO_PATH_V2 ?? `${testVideoRoot}/02.mp4`),
  resolve(process.env.FCR_E2E_VIDEO_PATH_V3 ?? `${testVideoRoot}/03.mp4`),
] as const;
const isWithinRoot = (root: string, candidate: string) => {
  const relativePath = relative(root, candidate);
  return relativePath !== '..' && !relativePath.startsWith(`..${sep}`) && !isAbsolute(relativePath);
};
if (videoPaths.some((videoPath) => !isWithinRoot(testVideoRoot, videoPath))) {
  throw new Error(`real-stack E2E videos must come from ${testVideoRoot}`);
}
const apiBaseUrl = process.env.FCR_E2E_API_BASE_URL;
if (!apiBaseUrl) {
  throw new Error('FCR_E2E_API_BASE_URL is required for the real-stack E2E profile');
}
if (process.env.FCR_E2E_DISPOSABLE_DATABASE !== '1') {
  throw new Error('FCR_E2E_DISPOSABLE_DATABASE=1 is required; real-stack E2E must not use the application database');
}
const projectRefId = process.env.FCR_E2E_PROJECT_REF_ID;
const projectName = process.env.FCR_E2E_PROJECT_NAME;
if (!projectRefId || !projectName) {
  throw new Error('FCR_E2E_PROJECT_REF_ID and FCR_E2E_PROJECT_NAME are required for the project-scoped real-stack E2E profile');
}
const principalContext = process.env.FCR_E2E_PRINCIPAL_CONTEXT;
if (!principalContext) {
  throw new Error('FCR_E2E_PRINCIPAL_CONTEXT is required for the project-scoped real-stack E2E profile');
}

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

async function appendVersion(page: Page, label: 'V2' | 'V3', videoPath: string) {
  const panel = page.getByTestId('append-version-panel');
  await panel.getByTestId('append-version-file').setInputFiles(videoPath);
  await panel.getByRole('button', { name: `追加 ${label}` }).click();
  await expect(page.getByTestId('upload-progress')).toBeVisible();
  await expect(page.getByTestId(`version-${label}`)).toBeVisible();
}

test('real proxy, backend, and PostgreSQL persist one V1/V2/V3 chain', async ({ page, request }) => {
  await Promise.all(videoPaths.map((videoPath) => access(videoPath)));
  const canonicalVideoRoot = await realpath(testVideoRoot);
  const verifiedVideoPaths = await Promise.all(videoPaths.map((videoPath) => realpath(videoPath)));
  if (verifiedVideoPaths.some((videoPath) => !isWithinRoot(canonicalVideoRoot, videoPath))) {
    throw new Error(`real-stack E2E videos must resolve within ${canonicalVideoRoot}`);
  }
  const videoStats = await Promise.all(verifiedVideoPaths.map((videoPath) => stat(videoPath)));
  if (videoStats.some((videoStat) => !videoStat.isFile())) {
    throw new Error('real-stack E2E video inputs must resolve to regular files');
  }
  await assertPostgresRuntime(request);
  const expectedApiPrefix = apiBaseUrl.replace(/\/+$/, '');
  await page.route(`${expectedApiPrefix}/api/v1/**`, async (route) => {
    await route.continue({
      headers: {
        ...await route.request().allHeaders(),
        'X-Principal-Context': principalContext,
      },
    });
  });
  const businessRequestUrls: string[] = [];
  page.on('request', (browserRequest) => {
    const requestUrl = browserRequest.url();
    const pathname = new URL(requestUrl).pathname;
    if (pathname.includes('/api/v1/final-cut-review/') || pathname.includes('/api/v1/files/')) {
      businessRequestUrls.push(requestUrl);
    }
  });
  await page.goto(`/edit/projects/${projectRefId}`);
  await expect(page.locator('#root')).toHaveAttribute('data-review-runtime', 'http');
  await expect(page.getByRole('heading', { name: new RegExp(projectName) })).toBeVisible();

  const updatedProjectName = `${projectName} 已编辑`;
  await page.getByRole('button', { name: '编辑项目资料' }).click();
  await expect(page.getByLabel('项目编号')).toHaveAttribute('readonly');
  await page.getByLabel('项目名称').fill(updatedProjectName);
  await page.getByLabel('项目说明').fill('真实 PostgreSQL 项目元数据已编辑。');
  await page.getByRole('button', { name: '保存项目资料' }).click();
  await expect(page.getByText('项目资料已更新。')).toBeVisible();
  await expect(page.getByRole('heading', { name: new RegExp(updatedProjectName) })).toBeVisible();

  const createPanel = page.getByTestId('create-item-upload');
  await createPanel.getByTestId('create-item-file').setInputFiles(verifiedVideoPaths.slice(0, 2));
  const firstUploadRow = createPanel.getByTestId('upload-row-0');
  const secondUploadRow = createPanel.getByTestId('upload-row-1');
  await expect(firstUploadRow).toContainText('01.mp4');
  await expect(secondUploadRow).toContainText('02.mp4');
  await firstUploadRow.getByLabel('成片标题').fill('真实栈批量第一集');
  await firstUploadRow.getByLabel('集数').fill('38');
  await secondUploadRow.getByLabel('成片标题').fill('真实栈批量失败后重试');
  await secondUploadRow.getByLabel('集数').fill('38');
  await createPanel.getByRole('button', { name: '上传 V1' }).click();
  await expect(page.getByTestId('upload-progress')).toBeVisible();
  await expect(firstUploadRow).toHaveCount(0);
  await expect(secondUploadRow).toBeVisible();
  await expect(secondUploadRow.getByRole('alert')).toBeVisible();
  await secondUploadRow.getByLabel('集数').fill('39');
  await createPanel.getByRole('button', { name: '上传 V1' }).click();
  await expect(createPanel.getByTestId('create-item-upload-rows')).toHaveCount(0);

  const table = page.getByTestId('review-item-table');
  const firstBatchItem = table.locator('article').filter({ hasText: '真实栈批量第一集' });
  await firstBatchItem.getByRole('button', { name: /编辑成片元数据/ }).click();
  const metadataDialog = page.getByRole('dialog', { name: /编辑成片元数据/ });
  await expect(metadataDialog.getByLabel(/成片编号/)).toHaveAttribute('readonly');
  await metadataDialog.getByLabel('成片标题').fill('真实栈已编辑成片');
  await metadataDialog.getByLabel('集数').fill('38');
  await metadataDialog.getByRole('button', { name: '保存成片元数据' }).click();
  await expect(page.getByText('成片「真实栈已编辑成片」元数据已更新。')).toBeVisible();

  const updatedItem = table.locator('article').filter({ hasText: '真实栈已编辑成片' });
  await expect(updatedItem.getByText('真实栈已编辑成片', { exact: true })).toBeVisible();
  await expect(updatedItem).toContainText('第 38 集');
  await updatedItem.getByRole('link', { name: /查看与追加/ }).click();
  await expect(page.getByTestId('version-V1')).toBeVisible();

  await page.goto(page.url().replace('/edit/', '/review/'));
  await expect(page.getByRole('button', { name: '开始审阅' })).toHaveCount(0);
  await page.getByPlaceholder('针对当前版本输入意见').fill('V1 首条意见即隐式开始审阅');
  await page.getByRole('button', { name: '提交意见' }).click();
  await expect(page.getByText('意见已提交到当前版本。')).toBeVisible();
  await page.goto(page.url().replace('/review/', '/edit/'));
  await appendVersion(page, 'V2', verifiedVideoPaths[1]);

  await page.goto(page.url().replace('/edit/', '/review/'));
  await page.getByPlaceholder('针对当前版本输入意见').fill('V2 当前版本意见');
  await page.getByRole('button', { name: '提交意见' }).click();
  await expect(page.getByText('意见已提交到当前版本。')).toBeVisible();
  await page.goto(page.url().replace('/review/', '/edit/'));
  await appendVersion(page, 'V3', verifiedVideoPaths[2]);
  await expect(page.getByTestId('version-V1')).toBeVisible();
  await expect(page.getByTestId('version-V2')).toBeVisible();
  await expect(page.getByTestId('version-V3')).toBeVisible();

  await page.reload();
  await assertPostgresRuntime(request);
  await expect(page.locator('#root')).toHaveAttribute('data-review-runtime', 'http');
  await expect(page.getByTestId('version-V3')).toBeVisible();
  await expect(page.getByTestId('version-V2')).toBeVisible();
  await expect(page.getByTestId('version-V1')).toBeVisible();
  await expect(page.getByText('当前版本未修改 0')).toBeVisible();
  await expect(page.getByText('历史意见 2')).toBeVisible();
  await page.getByTestId('version-V1').click();
  await expect(page.getByTestId('decision-bar')).toContainText('历史版本只读');
  await expect(page.getByTestId('issue-form')).toHaveCount(0);
  await expect(
    page.locator('.fj-review-issue-card').filter({ hasText: 'V1 首条意见即隐式开始审阅' }).first(),
  ).toBeVisible();
  await page.getByTestId('version-V3').click();
  await expect(page.getByText('当前版本未修改 0')).toBeVisible();
  await expect(page.getByText('历史意见 2')).toBeVisible();

  await page.getByRole('link', { name: '返回项目' }).click();
  const disposableTitle = '真实栈批量失败后重试';
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

  await page.goto(page.url().replace('/review/', '/edit/'));
  const editorIssueCard = page.locator('.fj-review-issue-card').filter({ hasText: '真实栈第二修订' });
  await editorIssueCard.getByRole('button', { name: '标记已修改' }).click();
  await expect(editorIssueCard.getByText('已修改', { exact: true })).toBeVisible();
  await page.goto(page.url().replace('/edit/', '/review/'));
  const reopenedIssueCard = page.locator('.fj-review-issue-card').filter({ hasText: '真实栈第二修订' });
  await reopenedIssueCard.getByRole('button', { name: '重新打开为未修改' }).click();
  await expect(reopenedIssueCard.getByText('未修改', { exact: true })).toBeVisible();
  await expect(page.getByText('当前版本有未修改意见，仍可定稿')).toBeVisible();
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
