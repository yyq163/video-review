import { Buffer } from 'node:buffer';
import { expect, test, type Page } from '@playwright/test';

const TEST_VIDEO_V1 = {
  name: 'e2e-v1.mp4',
  mimeType: 'video/mp4',
  buffer: Buffer.from('e2e v1 upload fixture'),
};
const TEST_VIDEO_V2 = {
  name: 'e2e-v2.mp4',
  mimeType: 'video/mp4',
  buffer: Buffer.from('e2e v2 upload fixture'),
};

const ENTRY_VISIBILITY_SCENARIOS = [
  { entryMode: 'edit', surface: 'project list', path: '/edit/projects' },
  { entryMode: 'edit', surface: 'project detail', path: '/edit/projects/prj_seed_final_cut' },
  { entryMode: 'edit', surface: 'review workspace', path: '/edit/projects/prj_seed_final_cut/items/item_ep28' },
  { entryMode: 'review', surface: 'project list', path: '/review/projects' },
  { entryMode: 'review', surface: 'project detail', path: '/review/projects/prj_seed_final_cut' },
  { entryMode: 'review', surface: 'review workspace', path: '/review/projects/prj_seed_final_cut/items/item_ep28' },
] as const;

test.beforeEach(async ({ page }) => {
  await page.goto('/edit/projects');
  await expect(page.locator('#root')).toHaveAttribute('data-review-runtime', 'mock');
});

async function expectUploadProgressVisible(page: Page) {
  await expect(page.getByTestId('upload-progress')).toBeVisible();
  await expect(page.getByTestId('upload-progress')).toContainText('%');
  await expect(page.getByTestId('upload-progress')).toContainText(/校验文件|创建上传会话|上传分片|绑定成片记录|上传完成/);
}

async function resolveAllCurrentIssues(page: import('@playwright/test').Page) {
  for (const issueId of ['issue_v2_001', 'issue_v2_002', 'issue_v2_003']) {
    await page.getByTestId(`issue-${issueId}`).getByRole('button', { name: '解决当前版本意见' }).click();
  }
}

async function openFirstEditableReviewItem(page: Page) {
  const itemTable = page.getByTestId('review-item-table');
  await expect(itemTable).toBeVisible();
  await expect(itemTable.getByRole('button', { name: /删除分集/ }).first()).toBeVisible();
  await itemTable.getByRole('link', { name: /查看与追加/ }).first().click();
}

for (const scenario of ENTRY_VISIBILITY_SCENARIOS) {
  test(`${scenario.entryMode} ${scenario.surface} exposes the approved entry navigation`, async ({ page }) => {
    await page.goto(scenario.path);
    const entryNavigation = page.getByRole('navigation', { name: '入口切换' });
    const editEntry = entryNavigation.getByRole('link', { name: '剪辑入口', exact: true });
    const reviewEntry = entryNavigation.getByRole('link', { name: '成片审阅', exact: true });

    await expect(entryNavigation).toBeVisible();
    await expect(entryNavigation.getByRole('link')).toHaveCount(scenario.entryMode === 'edit' ? 1 : 2);
    await expect(editEntry).toHaveCount(1);

    if (scenario.entryMode === 'edit') {
      await expect(editEntry).toHaveAttribute('aria-current', 'page');
      await expect(reviewEntry).toHaveCount(0);
      return;
    }

    await expect(editEntry).not.toHaveAttribute('aria-current', 'page');
    await expect(reviewEntry).toHaveCount(1);
    await expect(reviewEntry).toHaveAttribute('aria-current', 'page');
    await editEntry.click();
    await expect(page).toHaveURL(/\/edit\/projects$/);

    const editNavigation = page.getByRole('navigation', { name: '入口切换' });
    await expect(editNavigation.getByRole('link')).toHaveCount(1);
    await expect(editNavigation.getByRole('link', { name: '剪辑入口', exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(editNavigation.getByRole('link', { name: '成片审阅', exact: true })).toHaveCount(0);
  });
}

test('edit entry creates project, uploads V1, and appends isolated V2', async ({ page }) => {
  await page.goto('/edit/projects');
  await expect(page.getByRole('link', { name: '剪辑入口' })).toBeVisible();
  await page.getByRole('button', { name: /新建项目/ }).click();
  await page.getByLabel('项目名称').fill('E2E 成片项目');
  await page.getByLabel('项目编码').fill('E2E-FJ');
  await page.getByLabel('项目说明').fill('用于验证剪辑入口创建项目和上传版本。');
  await page.getByRole('button', { name: '创建项目' }).click();
  await expect(page.getByRole('heading', { name: /E2E 成片项目/ })).toBeVisible();

  await page.getByTestId('create-item-upload').getByRole('button', { name: '上传 V1' }).click();
  await expect(page.getByTestId('create-item-file-error')).toContainText('请选择原片文件');
  await expect(page.getByTestId('review-player')).toHaveCount(0);
  await page.getByTestId('create-item-file').setInputFiles(TEST_VIDEO_V1);
  await page.getByTestId('create-item-upload').getByRole('button', { name: '上传 V1' }).click();
  await expectUploadProgressVisible(page);
  await openFirstEditableReviewItem(page);
  await expect(page.getByTestId('review-player')).toBeVisible();
  await expect(page.getByTestId('version-V1')).toBeVisible();
  await expect(page.getByText('剪辑入口仅可查看意见')).toBeVisible();
  await expect(page.getByTestId('finalize-current')).toHaveCount(0);

  await page.getByTestId('append-version-panel').locator('button', { hasText: '追加 V2' }).click();
  await expect(page.getByTestId('append-version-file-error')).toContainText('请选择原片文件');
  await page.getByTestId('append-version-file').setInputFiles(TEST_VIDEO_V2);
  await page.getByTestId('append-version-panel').locator('button', { hasText: '追加 V2' }).click();
  await expectUploadProgressVisible(page);
  await expect(page.getByText('V2 已追加，旧版本意见不会继承。')).toBeVisible();
  await expect(page.getByText('当前版本未解决 0')).toBeVisible();
});

test('review entry archives and restores a project without deleting existing items', async ({ page }) => {
  await page.goto('/edit/projects');
  const card = page.getByTestId('project-card-prj_seed_final_cut');
  await expect(card).toContainText('真千金是男的');
  await expect(card).toContainText('进行中');
  await expect(card.getByRole('button', { name: '归档' })).toHaveCount(0);

  await page.goto('/review/projects');
  const reviewCard = page.getByTestId('project-card-prj_seed_final_cut');
  await reviewCard.getByRole('button', { name: '归档' }).click();
  await expect(reviewCard).toContainText('已归档');
  await expect(reviewCard.getByRole('button', { name: '恢复' })).toBeVisible();
  await expect(reviewCard.getByRole('button', { name: '删除项目' })).toHaveCount(0);

  await expect(reviewCard).toContainText('已归档');
  await reviewCard.getByRole('link', { name: /成片审阅/ }).click();
  await expect(page.getByTestId('archived-readonly-notice')).toContainText('恢复项目后才能创建成片');
  await expect(page.getByRole('button', { name: '删除项目' })).toHaveCount(0);
  await expect(page.getByTestId('review-item-table')).toContainText('第 28 集 · 最终成片');
  await page.getByRole('link', { name: '查看' }).click();
  await expect(page.getByTestId('archived-workspace-readonly-notice')).toContainText('归档项目只读');
  await expect(page.getByTestId('review-player')).toBeVisible();
  await expect(page.getByTestId('issue-form')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '要求修改' })).toHaveCount(0);
  await expect(page.getByTestId('finalize-current')).toHaveCount(0);
  await expect(page.getByTestId('package-project')).toBeVisible();

  await page.getByRole('link', { name: '成片审阅' }).click();
  await page.getByTestId('project-card-prj_seed_final_cut').getByRole('link', { name: /成片审阅/ }).click();
  await page.getByRole('button', { name: '恢复项目' }).click();
  await expect(page.getByText('项目已恢复，可继续剪辑管理。')).toBeVisible();
  await expect(page.getByTestId('create-item-upload')).toHaveCount(0);
  await expect(page.getByTestId('review-item-table')).toContainText('第 28 集 · 最终成片');
});

test('review projects list soft-deletes a project with confirmation while edit entry hides delete', async ({ page }) => {
  await page.goto('/edit/projects');
  await page.getByRole('button', { name: /新建项目/ }).click();
  await page.getByLabel('项目名称').fill('E2E 删除项目');
  await page.getByLabel('项目编码').fill('E2E-DELETE');
  await page.getByLabel('项目说明').fill('用于验证成片审阅入口项目删除。');
  await page.getByRole('button', { name: '创建项目' }).click();
  await expect(page.getByRole('heading', { name: /E2E 删除项目/ })).toBeVisible();

  await page.goto('/edit/projects');
  const deleteCard = page.locator('.fj-review-project-card').filter({ hasText: 'E2E 删除项目' });
  await expect(deleteCard).toBeVisible();
  await expect(deleteCard.getByRole('button', { name: '删除项目' })).toHaveCount(0);

  await page.goto('/review/projects');
  const reviewDeleteCard = page.locator('.fj-review-project-card').filter({ hasText: 'E2E 删除项目' });
  await expect(reviewDeleteCard).toBeVisible();
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('确认删除项目');
    await dialog.accept();
  });
  await reviewDeleteCard.getByRole('button', { name: '删除项目' }).click();
  await expect(reviewDeleteCard).toHaveCount(0);
});

test('review workspace submits centered toast and soft-deletes only current-version issue', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await expect(page.getByTestId('review-player')).toBeVisible();

  const episodeStrip = page.getByTestId('episode-strip');
  await expect(episodeStrip).toContainText('剧集列表 (1)');
  await expect(episodeStrip.getByTestId('episode-item-item_ep28')).toHaveCount(1);
  await expect(episodeStrip).toContainText('2 个版本 · 当前 V2');
  await expect(page.getByTestId('historical-issues').getByRole('button', { name: '删除意见' })).toHaveCount(0);

  const panelMetrics = await page.evaluate(() => {
    const panel = document.querySelector('[data-testid="issue-panel"]')?.getBoundingClientRect();
    const scroll = document.querySelector('[data-testid="issue-panel-scroll"]');
    const episode = document.querySelector('[data-testid="episode-strip"]')?.getBoundingClientRect();
    if (!panel || !scroll || !episode) throw new Error('missing issue panel or episode strip');
    const panelStyle = getComputedStyle(document.querySelector('[data-testid="issue-panel"]') as Element);
    const scrollStyle = getComputedStyle(scroll);
    return {
      panelBottomDelta: Math.abs(panel.bottom - episode.bottom),
      panelBorderStyle: panelStyle.borderTopStyle,
      scrollOverflowY: scrollStyle.overflowY,
      scrollHasOwnViewport: scroll.clientHeight > 0 && scroll.scrollHeight > scroll.clientHeight,
    };
  });
  expect(panelMetrics.panelBottomDelta).toBeLessThan(2);
  expect(panelMetrics.panelBorderStyle).not.toBe('none');
  expect(panelMetrics.scrollOverflowY).toMatch(/auto|scroll/);
  expect(panelMetrics.scrollHasOwnViewport).toBe(true);

  await page.getByPlaceholder('针对当前版本输入意见').fill('E2E 删除候选意见');
  await page.getByRole('button', { name: '提交意见' }).click();
  const toast = page.getByTestId('player-toast');
  await expect(toast).toHaveText('意见已提交到当前版本。');
  const toastMetrics = await page.evaluate(() => {
    const toastRect = document.querySelector('[data-testid="player-toast"]')?.getBoundingClientRect();
    const playerRect = document.querySelector('[data-testid="review-player"]')?.getBoundingClientRect();
    if (!toastRect || !playerRect) throw new Error('missing toast or player');
    return {
      centerXDelta: Math.abs((toastRect.left + toastRect.right) / 2 - (playerRect.left + playerRect.right) / 2),
      centerYDelta: Math.abs((toastRect.top + toastRect.bottom) / 2 - (playerRect.top + playerRect.bottom) / 2),
    };
  });
  expect(toastMetrics.centerXDelta).toBeLessThan(8);
  expect(toastMetrics.centerYDelta).toBeLessThan(8);
  await expect(toast).toBeHidden({ timeout: 4000 });

  const createdCard = page.locator('.fj-review-issue-card').filter({ hasText: 'E2E 删除候选意见' });
  await expect(createdCard).toBeVisible();
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('确认删除意见');
    await dialog.accept();
  });
  await createdCard.getByRole('button', { name: '删除意见' }).click();
  await expect(page.getByText('意见已删除。')).toBeVisible();
  await expect(createdCard).toHaveCount(0);
  await expect(page).not.toHaveURL(/issue=/);
});

test('precise playback selects current version issue and only its annotation set', async ({ page }) => {
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await expect(page.getByTestId('issue-panel')).toContainText('V2 00:00:00:04');
  await expect(page.locator('.fj-review-player-controls input[aria-label="视频时间轴"]')).toHaveCount(0);
  await expect(page.getByTestId('video-edge-timeline').locator('input[aria-label="视频时间轴"]')).toHaveCount(1);
  const mainVideoAudio = await page.locator('.fj-review-video-frame video').evaluate((video) => ({
    muted: (video as HTMLVideoElement).muted,
    volume: (video as HTMLVideoElement).volume,
  }));
  expect(mainVideoAudio.muted).toBe(false);
  expect(mainVideoAudio.volume).toBeGreaterThan(0);

  await page.getByTestId('issue-issue_v2_001').getByRole('button', { name: /#002/ }).click();
  await expect(page).toHaveURL(/issue=issue_v2_001/);
  await expect(page.getByTestId('current-frame')).toHaveText('4');
  await expect(page.getByTestId('review-player')).toHaveAttribute('data-paused', 'true');
  await expect(page.getByTestId('issue-issue_v2_001')).toHaveClass(/is-selected/);
  await expect(page.getByTestId('timeline-marker-issue_v2_001')).toHaveClass(/is-selected/);
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-annotation-set-id', 'aset_issue_v2_001_001');
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-selected-issue-id', 'issue_v2_001');
  await page.getByRole('button', { name: '下一帧' }).click();
  await expect(page.getByTestId('current-frame')).toHaveText('5');
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-annotation-set-id', '');

  await page.getByTestId('timeline-marker-issue_v2_002').click();
  await expect(page).toHaveURL(/issue=issue_v2_002/);
  await expect(page.getByTestId('current-frame')).toHaveText('6');
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-annotation-set-id', 'aset_issue_v2_002_001');

  await page.getByRole('button', { name: '下一条意见' }).click();
  await expect(page).toHaveURL(/issue=issue_v2_003/);
  await expect(page.getByTestId('current-frame')).toHaveText('8');
  await page.getByRole('button', { name: '上一条意见' }).click();
  await expect(page).toHaveURL(/issue=issue_v2_002/);
  await expect(page.getByTestId('current-frame')).toHaveText('6');
});

test('annotation layer is restored over the actual video rectangle on wide viewport', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await page.getByTestId('issue-issue_v2_001').getByRole('button', { name: /#002/ }).click();
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-annotation-set-id', 'aset_issue_v2_001_001');

  const metrics = await page.evaluate(() => {
    const stage = document.querySelector('.fj-review-video-frame')?.getBoundingClientRect();
    const video = document.querySelector('.fj-review-video-frame video') as HTMLVideoElement | null;
    const layer = document.querySelector('[data-testid="saved-annotation-layer"]')?.getBoundingClientRect();
    if (!stage || !video || !layer) throw new Error('missing player stage, video, or annotation layer');
    const videoRect = video.getBoundingClientRect();
    const videoWidth = video.videoWidth || 1280;
    const videoHeight = video.videoHeight || 720;
    const scale = Math.min(stage.width / videoWidth, stage.height / videoHeight);
    const expected = {
      left: stage.left + (stage.width - videoWidth * scale) / 2,
      top: stage.top + (stage.height - videoHeight * scale) / 2,
      width: videoWidth * scale,
      height: videoHeight * scale,
    };
    return {
      leftDelta: Math.abs(layer.left - expected.left),
      topDelta: Math.abs(layer.top - expected.top),
      widthDelta: Math.abs(layer.width - expected.width),
      heightDelta: Math.abs(layer.height - expected.height),
      videoLeftDelta: Math.abs(videoRect.left - expected.left),
      videoTopDelta: Math.abs(videoRect.top - expected.top),
      videoWidthDelta: Math.abs(videoRect.width - expected.width),
      videoHeightDelta: Math.abs(videoRect.height - expected.height),
      objectFit: getComputedStyle(video).objectFit,
    };
  });
  expect(metrics.objectFit).toBe('contain');
  expect(metrics.leftDelta).toBeLessThan(1);
  expect(metrics.topDelta).toBeLessThan(1);
  expect(metrics.widthDelta).toBeLessThan(1);
  expect(metrics.heightDelta).toBeLessThan(1);
  expect(metrics.videoLeftDelta).toBeLessThan(1);
  expect(metrics.videoTopDelta).toBeLessThan(1);
  expect(metrics.videoWidthDelta).toBeLessThan(1);
  expect(metrics.videoHeightDelta).toBeLessThan(1);
});

test('resized workspace keeps document overflow off and uses one application scroll layer', async ({ page }) => {
  await page.setViewportSize({ width: 1080, height: 740 });
  await page.goto('/edit/projects/prj_seed_final_cut/items/item_ep28');
  await expect(page.getByTestId('review-player')).toBeVisible();
  await expect(page.getByTestId('annotation-toolbar-dock')).toBeVisible();
  await expect(page.getByTestId('annotation-toolbar')).toBeVisible();

  await expect(page.getByTestId('version-V2')).toBeHidden();
  await page.getByRole('button', { name: '展开版本栏' }).click();
  await expect(page.getByTestId('version-V2')).toBeVisible();
  await page.getByRole('button', { name: '折叠版本栏' }).click();
  await expect(page.getByTestId('version-V2')).toBeHidden();

  await expect(page.getByTestId('issue-panel')).toBeHidden();
  await page.getByRole('button', { name: '打开意见栏' }).click();
  await expect(page.getByRole('dialog', { name: '意见反馈' })).toBeVisible();
  await page.locator('.fj-review-issue-drawer-close').click();
  await expect(page.getByTestId('issue-panel')).toBeHidden();

  const metrics = await page.evaluate(() => {
    const scrolling = document.scrollingElement;
    const workspace = document.querySelector('.fj-review-workspace');
    const workbench = document.querySelector('.fj-review-workbench');
    const mainColumn = document.querySelector('.fj-review-main-column');
    const versionRail = document.querySelector('.fj-review-version-rail');
    const issuePanel = document.querySelector('.fj-review-issue-panel');
    const comparePanel = document.querySelector('[data-testid="version-compare-panel"]');
    const stage = document.querySelector('.fj-review-player-stage');
    const frame = document.querySelector('.fj-review-video-frame');
    const mainVideo = document.querySelector('.fj-review-video-frame video') as HTMLVideoElement | null;
    const annotationLayer = document.querySelector('[data-testid="saved-annotation-layer"]');
    const annotationToolbarDock = document.querySelector('[data-testid="annotation-toolbar-dock"]');
    const annotationToolbar = document.querySelector('[data-testid="annotation-toolbar"]');
    const workspaceHead = document.querySelector('.fj-review-workspace-head');
    const toolbarInMainColumn = Boolean(document.querySelector('.fj-review-main-column [data-testid="annotation-toolbar-dock"]'));
    const playerControls = document.querySelector('.fj-review-player-controls');
    const playerShell = document.querySelector('[data-testid="review-player"]');
    if (
      !scrolling ||
      !workspace ||
      !workbench ||
      !mainColumn ||
      !versionRail ||
      !issuePanel ||
      !comparePanel ||
      !stage ||
      !frame ||
      !mainVideo ||
      !annotationLayer ||
      !annotationToolbarDock ||
      !annotationToolbar ||
      !workspaceHead ||
      !playerControls ||
      !playerShell
    ) {
      throw new Error('missing workspace layout nodes');
    }
    const workspaceStyle = getComputedStyle(workspace);
    const mainVideoStyle = getComputedStyle(mainVideo);
    const stageRect = stage.getBoundingClientRect();
    const frameRect = frame.getBoundingClientRect();
    const mainVideoRect = mainVideo.getBoundingClientRect();
    const videoWidth = mainVideo.videoWidth || 1280;
    const videoHeight = mainVideo.videoHeight || 720;
    const scale = Math.min(frameRect.width / videoWidth, frameRect.height / videoHeight);
    const expectedVideoRect = {
      left: frameRect.left + (frameRect.width - videoWidth * scale) / 2,
      top: frameRect.top + (frameRect.height - videoHeight * scale) / 2,
      width: videoWidth * scale,
      height: videoHeight * scale,
    };
    const annotationRect = annotationLayer.getBoundingClientRect();
    const workbenchRect = workbench.getBoundingClientRect();
    const mainColumnRect = mainColumn.getBoundingClientRect();
    const versionRailRect = versionRail.getBoundingClientRect();
    const issuePanelRect = issuePanel.getBoundingClientRect();
    const comparePanelRect = comparePanel.getBoundingClientRect();
    const annotationToolbarDockRect = annotationToolbarDock.getBoundingClientRect();
    const annotationToolbarRect = annotationToolbar.getBoundingClientRect();
    const controlsRect = playerControls.getBoundingClientRect();
    const playerShellRect = playerShell.getBoundingClientRect();
    const horizontalGap = Math.max(0, frameRect.width - expectedVideoRect.width);
    const verticalGap = Math.max(0, frameRect.height - expectedVideoRect.height);
    const comparePriorBottom = Math.max(workbenchRect.bottom, mainColumnRect.bottom, versionRailRect.bottom, issuePanelRect.bottom);
    const toolbarOverlapsFrame =
      annotationToolbarRect.left < frameRect.right &&
      annotationToolbarRect.right > frameRect.left &&
      annotationToolbarRect.top < frameRect.bottom &&
      annotationToolbarRect.bottom > frameRect.top;
    return {
      documentHasVerticalScroll: scrolling.scrollHeight > scrolling.clientHeight + 1,
      documentHasHorizontalScroll: scrolling.scrollWidth > scrolling.clientWidth + 1 || document.body.scrollWidth > window.innerWidth + 1,
      workspaceCanScroll: workspace.scrollHeight > workspace.clientHeight + 1,
      workspaceOverflowY: workspaceStyle.overflowY,
      mainVideoObjectFit: mainVideoStyle.objectFit,
      frameFillsStage:
        Math.abs(frameRect.left - stageRect.left) < 1 &&
        Math.abs(frameRect.top - stageRect.top) < 1 &&
        Math.abs(frameRect.width - stageRect.width) < 1 &&
        Math.abs(frameRect.height - stageRect.height) < 1,
      singleAxisLetterbox: horizontalGap < 1 || verticalGap < 1,
      horizontalGap,
      verticalGap,
      mainVideoMatchesContainedVideo:
        Math.abs(mainVideoRect.left - expectedVideoRect.left) < 1 &&
        Math.abs(mainVideoRect.top - expectedVideoRect.top) < 1 &&
        Math.abs(mainVideoRect.width - expectedVideoRect.width) < 1 &&
        Math.abs(mainVideoRect.height - expectedVideoRect.height) < 1,
      annotationMatchesContainedVideo:
        Math.abs(annotationRect.left - expectedVideoRect.left) < 1 &&
        Math.abs(annotationRect.top - expectedVideoRect.top) < 1 &&
        Math.abs(annotationRect.width - expectedVideoRect.width) < 1 &&
        Math.abs(annotationRect.height - expectedVideoRect.height) < 1,
      toolbarDocked: playerShell.getAttribute('data-toolbar-docked') === 'true',
      toolbarInsideDock:
        annotationToolbarRect.left >= annotationToolbarDockRect.left - 1 &&
        annotationToolbarRect.right <= annotationToolbarDockRect.right + 1 &&
        annotationToolbarRect.top >= annotationToolbarDockRect.top - 1 &&
        annotationToolbarRect.bottom <= annotationToolbarDockRect.bottom + 1,
      toolbarInWorkspaceHead: workspaceHead.contains(annotationToolbarDock),
      toolbarInMainColumn,
      toolbarAbovePlayer: annotationToolbarRect.bottom <= playerShellRect.top + 1,
      toolbarOverlapsFrame,
      compareStartsAfterPriorContent: comparePanelRect.top >= comparePriorBottom,
      compareOverlapsMainColumn: comparePanelRect.top < mainColumnRect.bottom && comparePanelRect.bottom > mainColumnRect.top,
      compareOverlapsIssuePanel: comparePanelRect.top < issuePanelRect.bottom && comparePanelRect.bottom > issuePanelRect.top,
      comparePriorBottom,
      compareTop: comparePanelRect.top,
      workbenchRight: workbenchRect.right,
      controlsBottom: controlsRect.bottom,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
    };
  });

  expect(metrics.documentHasVerticalScroll).toBe(false);
  expect(metrics.documentHasHorizontalScroll).toBe(false);
  expect(metrics.workspaceCanScroll).toBe(true);
  expect(metrics.workspaceOverflowY).toMatch(/auto|scroll/);
  expect(metrics.mainVideoObjectFit).toBe('contain');
  expect(metrics.frameFillsStage).toBe(true);
  expect(metrics.singleAxisLetterbox).toBe(true);
  expect(metrics.mainVideoMatchesContainedVideo).toBe(true);
  expect(metrics.annotationMatchesContainedVideo).toBe(true);
  expect(metrics.toolbarDocked).toBe(true);
  expect(metrics.toolbarInsideDock).toBe(true);
  expect(metrics.toolbarInWorkspaceHead).toBe(true);
  expect(metrics.toolbarInMainColumn).toBe(true);
  expect(metrics.toolbarAbovePlayer).toBe(true);
  expect(metrics.toolbarOverlapsFrame).toBe(false);
  expect(metrics.compareStartsAfterPriorContent).toBe(true);
  expect(metrics.compareOverlapsMainColumn).toBe(false);
  expect(metrics.compareOverlapsIssuePanel).toBe(false);
  expect(metrics.compareTop).toBeGreaterThanOrEqual(metrics.comparePriorBottom);
  expect(metrics.workbenchRight).toBeLessThanOrEqual(metrics.viewportWidth);
  expect(metrics.controlsBottom).toBeLessThanOrEqual(metrics.viewportHeight);
});

test('desktop breakpoint keeps issue panel beside the main player at 1366 and 1440', async ({ page }) => {
  for (const viewport of [
    { width: 1366, height: 768 },
    { width: 1440, height: 900 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
    await expect(page.getByTestId('review-player')).toBeVisible();
    await expect(page.getByTestId('version-V2')).toBeVisible();
    await expect(page.getByTestId('issue-panel')).toBeVisible();

    const metrics = await page.evaluate(() => {
      const workbench = document.querySelector('.fj-review-workbench')?.getBoundingClientRect();
      const mainColumn = document.querySelector('.fj-review-main-column')?.getBoundingClientRect();
      const versionRail = document.querySelector('.fj-review-version-rail')?.getBoundingClientRect();
      const issuePanel = document.querySelector('.fj-review-issue-panel')?.getBoundingClientRect();
      const frame = document.querySelector('.fj-review-video-frame')?.getBoundingClientRect();
      if (!workbench || !mainColumn || !versionRail || !issuePanel || !frame) {
        throw new Error('missing desktop layout nodes');
      }
      return {
        issueRightOfMain: issuePanel.left >= versionRail.right - 1 && issuePanel.left > mainColumn.left,
        issueSameRow: Math.abs(issuePanel.top - mainColumn.top) < 2,
        versionRailBetweenMainAndIssue: versionRail.left >= mainColumn.right - 1 && versionRail.right <= issuePanel.left + 1,
        mainColumnPriorityRatio: mainColumn.width / workbench.width,
        frameWidth: frame.width,
        documentHasHorizontalScroll:
          document.documentElement.scrollWidth > window.innerWidth + 1 || document.body.scrollWidth > window.innerWidth + 1,
      };
    });

    expect(metrics.issueRightOfMain).toBe(true);
    expect(metrics.issueSameRow).toBe(true);
    expect(metrics.versionRailBetweenMainAndIssue).toBe(true);
    expect(metrics.mainColumnPriorityRatio).toBeGreaterThan(0.58);
    expect(metrics.frameWidth).toBeGreaterThan(700);
    expect(metrics.documentHasHorizontalScroll).toBe(false);
  }
});

test('ultrawide viewport keeps the workstation non-full-width, topbar-aligned, and the append V2 CTA fills its action rail', async ({
  page,
}) => {
  const suffix = Date.now().toString().slice(-6);
  await page.setViewportSize({ width: 2048, height: 1152 });
  await page.goto('/edit/projects');
  await page.getByRole('button', { name: /新建项目/ }).click();
  await page.getByLabel('项目名称').fill(`E2E 顶栏对齐 ${suffix}`);
  await page.getByLabel('项目编码').fill(`E2E-WIDTH-${suffix}`);
  await page.getByLabel('项目说明').fill('用于验证主界面宽度跟顶栏对齐以及追加 V2 按钮规格。');
  await page.getByRole('button', { name: '创建项目' }).click();
  await page.getByTestId('create-item-file').setInputFiles(TEST_VIDEO_V1);
  await page.getByTestId('create-item-upload').getByRole('button', { name: '上传 V1' }).click();
  await openFirstEditableReviewItem(page);
  await expect(page.getByTestId('review-player')).toBeVisible();
  await expect(page.getByRole('button', { name: '确认追加 V2' })).toBeVisible();

  const metrics = await page.evaluate(() => {
    const root = document.querySelector('.fj-review-root')?.getBoundingClientRect();
    const topbar = document.querySelector('.fj-review-topbar')?.getBoundingClientRect();
    const topbarInner = document.querySelector('.fj-review-topbar-inner')?.getBoundingClientRect();
    const workspace = document.querySelector('.fj-review-workspace')?.getBoundingClientRect();
    const workspaceFrame = document.querySelector('[data-testid="review-workspace-frame"]')?.getBoundingClientRect();
    const workbench = document.querySelector('.fj-review-workbench')?.getBoundingClientRect();
    const appendButton = [...document.querySelectorAll('button')]
      .find((button) => button.textContent?.includes('确认追加 V2'))
      ?.getBoundingClientRect();
    const appendFile = document.querySelector('.fj-review-inline-upload-file')?.getBoundingClientRect();
    const appendReason = document.querySelector('.fj-review-inline-upload-reason')?.getBoundingClientRect();
    if (!root || !topbar || !topbarInner || !workspace || !workspaceFrame || !workbench || !appendButton || !appendFile || !appendReason) {
      throw new Error('missing root, topbar, workspace, workbench, or append button');
    }
    return {
      rootWidth: root.width,
      topbarWidth: topbar.width,
      topbarInnerWidth: topbarInner.width,
      workspaceWidth: workspace.width,
      workspaceFrameWidth: workspaceFrame.width,
      frameIsBounded: workspaceFrame.width < window.innerWidth - 160,
      frameIsCentered: Math.abs(workspaceFrame.left - (window.innerWidth - workspaceFrame.width) / 2),
      workbenchLeftDeltaToTopbarInner: Math.abs(workbench.left - topbarInner.left),
      workbenchRightDeltaToTopbarInner: Math.abs(workbench.right - topbarInner.right),
      appendButtonWidth: appendButton.width,
      appendButtonHeight: appendButton.height,
      appendButtonTopDeltaToFirstField: Math.abs(appendButton.top - appendFile.top),
      appendButtonBottomDeltaToSecondField: Math.abs(appendButton.bottom - appendReason.bottom),
      documentHasHorizontalScroll:
        document.documentElement.scrollWidth > window.innerWidth + 1 || document.body.scrollWidth > window.innerWidth + 1,
    };
  });

  expect(Math.abs(metrics.rootWidth - metrics.topbarWidth)).toBeLessThanOrEqual(1);
  expect(metrics.workspaceWidth).toBeLessThanOrEqual(metrics.topbarWidth);
  expect(Math.abs(metrics.workspaceFrameWidth - metrics.topbarInnerWidth)).toBeLessThanOrEqual(1);
  expect(metrics.frameIsBounded).toBe(true);
  expect(metrics.frameIsCentered).toBeLessThanOrEqual(12);
  expect(metrics.workbenchLeftDeltaToTopbarInner).toBeLessThanOrEqual(1);
  expect(metrics.workbenchRightDeltaToTopbarInner).toBeLessThanOrEqual(1);
  expect(metrics.appendButtonWidth).toBeGreaterThanOrEqual(100);
  expect(metrics.appendButtonHeight).toBeGreaterThan(90);
  expect(metrics.appendButtonTopDeltaToFirstField).toBeLessThanOrEqual(1);
  expect(metrics.appendButtonBottomDeltaToSecondField).toBeLessThanOrEqual(1);
  expect(metrics.documentHasHorizontalScroll).toBe(false);
});

test('review workspace can be keyboard-scrolled from player controls to version compare', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 776 });
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  const workspace = page.getByTestId('review-workspace-scroll-region');
  await expect(workspace).toBeVisible();
  await expect(page.getByTestId('version-compare-panel')).toBeAttached();

  const autoPause = page.getByLabel('自动暂停');
  await autoPause.focus();
  await expect(autoPause).toBeFocused();
  let compareVisiblePixels = 0;
  for (let i = 0; i < 8; i += 1) {
    await page.keyboard.press('PageDown');
    await page.waitForTimeout(100);
    compareVisiblePixels = await page.getByTestId('version-compare-panel').evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return Math.max(0, Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0));
    });
    if (compareVisiblePixels > 120) break;
  }

  await expect
    .poll(async () => workspace.evaluate((node) => node.scrollTop), { message: 'workspace should scroll after PageDown keys' })
    .toBeGreaterThan(0);
  expect(compareVisiblePixels).toBeGreaterThan(120);
});

test('manual seek and keyboard shortcuts do not trigger auto-pause issue selection', async ({ page }) => {
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await page.getByLabel('倍速').selectOption('0.75');
  await expect(page.getByLabel('倍速')).toHaveValue('0.75');

  await page.keyboard.press('ArrowRight');
  await expect(page.getByTestId('current-frame')).toHaveText('1');
  await page.keyboard.press('Shift+ArrowRight');
  await expect(page.getByTestId('current-frame')).toHaveText('11');

  await page.getByLabel('时间码输入').fill('00:00:00:05');
  await page.getByRole('button', { name: '跳转' }).click();
  await expect(page.getByTestId('current-frame')).toHaveText('5');
  const hiddenTimeDisplays = await page.evaluate(() => {
    return ['current-timecode', 'current-frame', 'duration-timecode'].map((testId) => {
      const element = document.querySelector(`[data-testid="${testId}"]`);
      if (!element) throw new Error(`missing ${testId}`);
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return {
        testId,
        className: element.className,
        position: style.position,
        clipPath: style.clipPath,
        overflow: style.overflow,
        width: rect.width,
        height: rect.height,
      };
    });
  });
  for (const metric of hiddenTimeDisplays) {
    expect(metric.className).toContain('fj-review-sr-only');
    expect(metric.position).toBe('absolute');
    expect(metric.clipPath).toBe('inset(50%)');
    expect(metric.overflow).toBe('hidden');
    expect(metric.width).toBeLessThanOrEqual(1);
    expect(metric.height).toBeLessThanOrEqual(1);
  }
  await expect(page).not.toHaveURL(/issue=/);
});

test('desktop workspace docks annotation toolbar in the main column above the video', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 776 });
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await expect(page.getByTestId('review-player')).toBeVisible();
  await expect(page.getByTestId('annotation-toolbar-dock')).toBeVisible();
  await expect(page.getByTestId('annotation-toolbar')).toBeVisible();

  const metrics = await page.evaluate(() => {
    const toolbar = document.querySelector('[data-testid="annotation-toolbar"]')?.getBoundingClientRect();
    const dock = document.querySelector('[data-testid="annotation-toolbar-dock"]')?.getBoundingClientRect();
    const frame = document.querySelector('.fj-review-video-frame')?.getBoundingClientRect();
    const workspaceHead = document.querySelector('.fj-review-workspace-head');
    const dockNode = document.querySelector('[data-testid="annotation-toolbar-dock"]');
    const workbench = document.querySelector('.fj-review-workbench')?.getBoundingClientRect();
    const mainColumn = document.querySelector('.fj-review-main-column')?.getBoundingClientRect();
    const player = document.querySelector('[data-testid="review-player"]')?.getBoundingClientRect();
    const playerNode = document.querySelector('[data-testid="review-player"]');
    if (!toolbar || !dock || !frame || !workspaceHead || !dockNode || !workbench || !mainColumn || !player || !playerNode) {
      throw new Error('missing toolbar dock or video frame');
    }
    const toolbarOverlapsFrame =
      toolbar.left < frame.right && toolbar.right > frame.left && toolbar.top < frame.bottom && toolbar.bottom > frame.top;
    return {
      toolbarDocked: playerNode.getAttribute('data-toolbar-docked') === 'true',
      toolbarInsideDock:
        toolbar.left >= dock.left - 1 &&
        toolbar.right <= dock.right + 1 &&
        toolbar.top >= dock.top - 1 &&
        toolbar.bottom <= dock.bottom + 1,
      toolbarRightAlignedToDock: Math.abs(toolbar.right - dock.right) < 2,
      toolbarInWorkspaceHead: workspaceHead.contains(dockNode),
      toolbarInMainColumn: Boolean(document.querySelector('.fj-review-main-column [data-testid="annotation-toolbar-dock"]')),
      toolbarAbovePlayer: toolbar.bottom <= player.top + 1,
      toolbarOverlapsFrame,
      mainColumnPriorityRatio: mainColumn.width / workbench.width,
      playerHeightRatio: player.height / window.innerHeight,
      documentHasHorizontalScroll:
        document.documentElement.scrollWidth > window.innerWidth + 1 || document.body.scrollWidth > window.innerWidth + 1,
    };
  });

  expect(metrics.toolbarDocked).toBe(true);
  expect(metrics.toolbarInsideDock).toBe(true);
  expect(metrics.toolbarRightAlignedToDock).toBe(true);
  expect(metrics.toolbarInWorkspaceHead).toBe(true);
  expect(metrics.toolbarInMainColumn).toBe(true);
  expect(metrics.toolbarAbovePlayer).toBe(true);
  expect(metrics.toolbarOverlapsFrame).toBe(false);
  expect(metrics.mainColumnPriorityRatio).toBeGreaterThan(0.58);
  expect(metrics.playerHeightRatio).toBeGreaterThan(0.42);
  expect(metrics.playerHeightRatio).toBeLessThan(0.7);
  expect(metrics.documentHasHorizontalScroll).toBe(false);
});

test('narrow workspace keeps annotation toolbar docked above the video', async ({ page }) => {
  await page.setViewportSize({ width: 880, height: 760 });
  await page.goto('/edit/projects/prj_seed_final_cut/items/item_ep28');
  await expect(page.getByTestId('review-player')).toBeVisible();
  await expect(page.getByTestId('annotation-toolbar-dock')).toBeVisible();
  await expect(page.getByTestId('annotation-toolbar')).toBeVisible();

  const metrics = await page.evaluate(() => {
    const toolbar = document.querySelector('[data-testid="annotation-toolbar"]')?.getBoundingClientRect();
    const dock = document.querySelector('[data-testid="annotation-toolbar-dock"]')?.getBoundingClientRect();
    const frame = document.querySelector('.fj-review-video-frame')?.getBoundingClientRect();
    const playerControls = document.querySelector('.fj-review-player-controls')?.getBoundingClientRect();
    const player = document.querySelector('[data-testid="review-player"]');
    if (!toolbar || !dock || !frame || !playerControls || !player) throw new Error('missing toolbar dock or video frame');
    const toolbarOverlapsFrame =
      toolbar.left < frame.right && toolbar.right > frame.left && toolbar.top < frame.bottom && toolbar.bottom > frame.top;
    return {
      toolbarDocked: player.getAttribute('data-toolbar-docked') === 'true',
      toolbarInsideDock:
        toolbar.left >= dock.left - 1 &&
        toolbar.right <= dock.right + 1 &&
        toolbar.top >= dock.top - 1 &&
        toolbar.bottom <= dock.bottom + 1,
      toolbarRightAlignedToDock: Math.abs(toolbar.right - dock.right) < 2,
      toolbarAboveFrame: toolbar.bottom <= frame.top + 1,
      toolbarOverlapsFrame,
      controlsBottom: playerControls.bottom,
      viewportHeight: window.innerHeight,
      documentHasHorizontalScroll:
        document.documentElement.scrollWidth > window.innerWidth + 1 || document.body.scrollWidth > window.innerWidth + 1,
    };
  });

  expect(metrics.toolbarDocked).toBe(true);
  expect(metrics.toolbarInsideDock).toBe(true);
  expect(metrics.toolbarRightAlignedToDock).toBe(true);
  expect(metrics.toolbarAboveFrame).toBe(true);
  expect(metrics.toolbarOverlapsFrame).toBe(false);
  expect(metrics.controlsBottom).toBeLessThanOrEqual(metrics.viewportHeight);
  expect(metrics.documentHasHorizontalScroll).toBe(false);
});

test('manual version compare shows dual players with independent issue layers', async ({ page }) => {
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await expect(page.getByTestId('version-compare-panel')).toBeVisible();
  await expect(page.getByTestId('version-compare-left')).toContainText('V1');
  await expect(page.getByTestId('version-compare-right')).toContainText('V2');
  await expect(page.getByLabel('左侧版本播放器 V1')).toBeVisible();
  await expect(page.getByLabel('右侧版本播放器 V2')).toBeVisible();
  await expect(page.getByTestId('version-compare-left-annotation-layer')).toBeVisible();
  await expect(page.getByTestId('version-compare-right-annotation-layer')).toBeVisible();
  const compareLayout = await page.evaluate(() => {
    const leftFrame = document.querySelector('[data-testid="version-compare-left"] .fj-review-compare-frame')?.getBoundingClientRect();
    const rightFrame = document.querySelector('[data-testid="version-compare-right"] .fj-review-compare-frame')?.getBoundingClientRect();
    const leftMeta = document.querySelector('[data-testid="version-compare-left"] dl')?.getBoundingClientRect();
    const rightMeta = document.querySelector('[data-testid="version-compare-right"] dl')?.getBoundingClientRect();
    const leftVideo = document.querySelector('[aria-label="左侧版本播放器 V1"]') as HTMLVideoElement | null;
    const rightVideo = document.querySelector('[aria-label="右侧版本播放器 V2"]') as HTMLVideoElement | null;
    const rightLayer = document.querySelector('[data-testid="version-compare-right-annotation-layer"]');
    if (!leftFrame || !rightFrame || !leftMeta || !rightMeta || !leftVideo || !rightVideo || !rightLayer) {
      throw new Error('missing compare frame, metadata, video, or annotation layer');
    }
    return {
      heightDelta: Math.abs(leftFrame.height - rightFrame.height),
      topDelta: Math.abs(leftFrame.top - rightFrame.top),
      metaTopDelta: Math.abs(leftMeta.top - rightMeta.top),
      leftHeight: leftFrame.height,
      rightHeight: rightFrame.height,
      leftObjectFit: getComputedStyle(leftVideo).objectFit,
      rightObjectFit: getComputedStyle(rightVideo).objectFit,
      arrowHaloHeads: rightLayer.querySelectorAll('path[stroke="rgba(0,0,0,0.72)"]').length,
      arrowColorHeads: rightLayer.querySelectorAll('path[stroke="#ffcc3d"]').length,
      arrowHaloLines: rightLayer.querySelectorAll('line[stroke="rgba(0,0,0,0.72)"]').length,
      arrowColorLines: rightLayer.querySelectorAll('line[stroke="#ffcc3d"]').length,
    };
  });
  expect(compareLayout.heightDelta).toBeLessThan(1);
  expect(compareLayout.topDelta).toBeLessThan(1);
  expect(compareLayout.metaTopDelta).toBeLessThan(1);
  expect(compareLayout.leftHeight).toBeGreaterThan(200);
  expect(compareLayout.rightHeight).toBeGreaterThan(200);
  expect(compareLayout.leftObjectFit).toBe('contain');
  expect(compareLayout.rightObjectFit).toBe('contain');
  expect(compareLayout.arrowHaloHeads).toBeGreaterThanOrEqual(1);
  expect(compareLayout.arrowColorHeads).toBeGreaterThanOrEqual(1);
  expect(compareLayout.arrowHaloLines).toBeGreaterThanOrEqual(1);
  expect(compareLayout.arrowColorLines).toBeGreaterThanOrEqual(1);

  await page.getByLabel('同步播放').check();
  const leftVideo = page.getByLabel('左侧版本播放器 V1');
  const rightVideo = page.getByLabel('右侧版本播放器 V2');
  await leftVideo.evaluate((node) => {
    const video = node as HTMLVideoElement;
    video.currentTime = 0.2;
    video.dispatchEvent(new Event('seeked'));
  });
  await expect.poll(() => rightVideo.evaluate((node) => (node as HTMLVideoElement).currentTime)).toBeGreaterThan(0.15);
  await page.waitForTimeout(120);
  await rightVideo.evaluate((node) => {
    const video = node as HTMLVideoElement;
    video.currentTime = 0.32;
    video.dispatchEvent(new Event('seeked'));
  });
  await expect.poll(() => leftVideo.evaluate((node) => (node as HTMLVideoElement).currentTime)).toBeGreaterThan(0.27);
  await rightVideo.click();
  await expect.poll(() => rightVideo.evaluate((node) => (node as HTMLVideoElement).paused)).toBe(false);
  await expect.poll(() => leftVideo.evaluate((node) => (node as HTMLVideoElement).paused)).toBe(false);
  await rightVideo.click();
  await expect.poll(() => rightVideo.evaluate((node) => (node as HTMLVideoElement).paused)).toBe(true);
  await expect.poll(() => leftVideo.evaluate((node) => (node as HTMLVideoElement).paused)).toBe(true);
  const driftAfterPause = await leftVideo.evaluate((leftNode) => {
    const rightNode = document.querySelector('[aria-label="右侧版本播放器 V2"]') as HTMLVideoElement | null;
    return Math.abs((leftNode as HTMLVideoElement).currentTime - (rightNode?.currentTime ?? 0));
  });
  expect(driftAfterPause).toBeLessThan(0.25);
});

test('editing an issue creates a current revision and replies stay in thread', async ({ page }) => {
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  const issueCard = page.getByTestId('issue-issue_v2_001');
  await issueCard.getByRole('button', { name: /#002/ }).click();
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-annotation-set-id', 'aset_issue_v2_001_001');

  await issueCard.getByRole('button', { name: '编辑意见' }).click();
  await issueCard.getByLabel('编辑意见正文').fill('E2E 更新后的当前修订意见');
  await issueCard.getByRole('button', { name: '保存为新修订' }).click();
  await expect(page.getByText('意见已保存为新修订。')).toBeVisible();
  await expect(issueCard).toContainText('E2E 更新后的当前修订意见');
  await expect(issueCard).toContainText('R2');
  await issueCard.getByText('修订历史（2）').click();
  await expect(issueCard.getByText('历史修订只读')).toBeVisible();
  await expect(issueCard.getByText('V2 00:00:00:04 右侧字幕可再向内收 12px，避免贴边。')).toBeVisible();
  await expect(page.getByTestId('saved-annotation-layer')).not.toHaveAttribute('data-annotation-set-id', 'aset_issue_v2_001_001');
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-selected-issue-id', 'issue_v2_001');

  await issueCard.getByRole('button', { name: '回复' }).click();
  await issueCard.getByLabel('回复意见正文').fill('E2E 回复保持在线程，不改变回放目标');
  await issueCard.getByRole('button', { name: '提交回复' }).click();
  await expect(page.getByText('回复已提交。')).toBeVisible();
  await expect(issueCard).toContainText('E2E 回复保持在线程，不改变回放目标');
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-selected-issue-id', 'issue_v2_001');
});

test('auto pause selects current-version unresolved issue during natural playback', async ({ page }) => {
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await page.getByRole('button', { name: '播放' }).click();
  await expect(page).toHaveURL(/issue=issue_v2_001/);
  await expect(page.getByTestId('review-player')).toHaveAttribute('data-paused', 'true');
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-annotation-set-id', 'aset_issue_v2_001_001');
});

test('precise playback switches to historical version before restoring annotation set', async ({ page }) => {
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await expect(page.getByTestId('issue-panel')).toContainText('V2 00:00:00:04');

  await page.getByTestId('historical-issues').getByTestId('issue-issue_v1_001').getByRole('button', { name: /#001/ }).click();
  await expect(page).toHaveURL(/version=ver_ep28_v1/);
  await expect(page).toHaveURL(/issue=issue_v1_001/);
  await expect(page.getByTestId('version-V1')).toHaveClass(/is-active/);
  await expect(page.getByTestId('current-frame')).toHaveText('2');
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-annotation-set-id', 'aset_issue_v1_001_001');
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-selected-issue-id', 'issue_v1_001');
  await expect(page.getByTestId('saved-annotation-layer')).not.toHaveAttribute('data-annotation-set-id', 'aset_issue_v2_001_001');
  await expect(page.getByTestId('issue-panel').getByText('历史版本只读').first()).toBeVisible();
});

test('rapid issue selection only applies the last playback target', async ({ page }) => {
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await page.getByTestId('issue-issue_v2_001').getByRole('button', { name: /#002/ }).click();
  await page.getByTestId('issue-issue_v2_002').getByRole('button', { name: /#003/ }).click();
  await page.getByTestId('issue-issue_v2_003').getByRole('button', { name: /#004/ }).click();

  await expect(page).toHaveURL(/issue=issue_v2_003/);
  await expect(page.getByTestId('current-frame')).toHaveText('8');
  await expect(page.getByTestId('saved-annotation-layer')).toHaveAttribute('data-annotation-set-id', 'aset_issue_v2_003_001');
  await expect(page.getByTestId('issue-issue_v2_003')).toHaveClass(/is-selected/);
  await expect(page.getByTestId('timeline-marker-issue_v2_003')).toHaveClass(/is-selected/);
});

test('version isolation and finalization use only current version unresolved issues', async ({ page }) => {
  await page.goto('/review/projects/prj_seed_final_cut/items/item_ep28');
  await expect(page.getByTestId('issue-panel')).toContainText('当前版本未解决 3');
  await expect(page.getByTestId('historical-issues')).toContainText('V1 00:00:00:02');
  await expect(page.getByTestId('finalize-current')).toBeDisabled();

  await resolveAllCurrentIssues(page);
  await expect(page.getByText('当前版本可定稿')).toBeVisible();
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('不可撤销');
    await dialog.dismiss();
  });
  await page.getByTestId('finalize-current').click();
  await expect(page.getByText('当前版本可定稿')).toBeVisible();
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('确认将当前版本定稿');
    await dialog.accept();
  });
  await page.getByTestId('finalize-current').click();
  await expect(page.getByText('当前版本已精确定稿冻结。')).toBeVisible();
  await expect(page.getByText('当前版本已定稿冻结')).toBeVisible();

  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: /下载单片定稿原片/ }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toContain('V2');

  await page.getByTestId('package-project').click();
  await expect(page.getByText('项目包下载就绪，包含 1 个定稿原片。')).toBeVisible();
  await expect(page.getByTestId('package-project')).toContainText('项目包下载就绪');
  const packageDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('package-project').click();
  const packageDownload = await packageDownloadPromise;
  expect(packageDownload.suggestedFilename()).toContain('finalized-originals.zip');
});

test('entry capability boundary hides review writes and project delete in edit entry while hiding project creation in review entry', async ({ page }) => {
  await page.goto('/edit/projects/prj_seed_final_cut/items/item_ep28');
  await expect(page.getByText('剪辑入口仅可查看意见')).toBeVisible();
  await expect(page.getByTestId('finalize-current')).toHaveCount(0);
  await expect(page.getByTestId('package-project')).toHaveCount(0);
  await page.goto('/edit/projects');
  await expect(page.getByTestId('project-card-prj_seed_final_cut').getByRole('button', { name: '删除项目' })).toHaveCount(0);

  await page.goto('/review/projects');
  await expect(page.getByRole('button', { name: /新建项目/ })).toHaveCount(0);
  await expect(page.getByTestId('project-card-prj_seed_final_cut').getByRole('button', { name: '删除项目' })).toBeVisible();
});
