import { forwardRef } from 'react';
import { CapabilityGate } from '../components/shared';
import { AppendVersionPanel } from '../components/UploadPanel';
import { ReviewPlayer, type ReviewPlayerHandle } from '../components/ReviewPlayer';
import { DecisionBar } from '../components/DecisionBar';
import type { ReviewWorkspaceController } from './review-workspace-controller';
import { EpisodeStrip } from './review-workspace-elements';

export const ReviewWorkspaceMainColumn = forwardRef<ReviewPlayerHandle, { controller: ReviewWorkspaceController }>(
  function ReviewWorkspaceMainColumn({ controller }, playerRef) {
    const { actions, data, mutations, playback, props } = controller;
    const packageState = mutations.createProjectFinalizedPackage.isPending
      ? 'preparing'
      : mutations.downloadProjectFinalizedPackage.isPending
        ? 'downloading'
        : mutations.createProjectFinalizedPackage.isError || mutations.downloadProjectFinalizedPackage.isError
          ? 'failed'
          : mutations.createProjectFinalizedPackage.data
            ? 'ready'
            : 'idle';

    return (
      <div className="fj-review-main-column">
        <div className="fj-review-workspace-head">
          <button
            className="fj-review-back-button"
            onClick={() => controller.navigate(`/${props.entryMode}/projects/${props.projectRefId}`)}
          >
            ← {data.item.episode}
          </button>
          <div className="fj-review-workspace-title">
            <strong>{data.item.title}</strong>
            <span>
              {data.project.name} · {data.currentVersion.label} · {data.currentVersion.sha256.slice(0, 12)}
            </span>
          </div>
          <div
            className="fj-review-toolbar-dock"
            data-testid="annotation-toolbar-dock"
            ref={controller.setAnnotationToolbarHost}
          />
        </div>
        <div className="fj-review-player-area">
          <ReviewPlayer
            key={data.currentVersion.versionId}
            ref={playerRef}
            version={data.currentVersion}
            issues={controller.currentIssues}
            selectedAnnotationSet={controller.selectedAnnotationSet}
            selectedIssueId={controller.selectedIssueId}
            initialTimeMs={controller.timeMs}
            annotationReadonlyReason={controller.writeReadonlyReason}
            annotationToolbarHost={controller.annotationToolbarHost}
            disableInlineAnnotationToolbar
            onTimeChange={controller.setTimeMs}
            onDraftChange={controller.setDraftShapes}
            onSelectIssue={playback.selectIssue}
            onPlaybackError={playback.setPlaybackError}
            onCreateIssueShortcut={() => actions.createIssue('快捷键创建当前时间码意见')}
          />
          {controller.toast && (
            <button
              className="fj-review-player-toast"
              data-testid="player-toast"
              onClick={() => controller.setToast(null)}
              type="button"
            >
              {controller.toast}
            </button>
          )}
        </div>
        {controller.readonlyReason ? (
          <section className="fj-review-readonly-notice" data-testid="archived-workspace-readonly-notice">
            <strong>项目已归档</strong>
            <span>{controller.readonlyReason}</span>
          </section>
        ) : null}
        <DecisionBar
          entryMode={props.entryMode}
          version={data.currentVersion}
          issues={controller.currentIssues}
          finalization={data.activeFinalization}
          isCurrentVersion={controller.isSelectedCurrent}
          readonlyReason={controller.readonlyReason}
          pending={controller.pending}
          packageState={packageState}
          onFinalize={actions.finalize}
          onDownload={actions.download}
          onPackage={actions.packageProject}
        />
        {controller.canAppendVersion && controller.appendVersionConfirmationRequired ? (
          <section className="fj-review-readonly-notice" data-testid="append-version-confirmation-required" role="alert">
            {controller.appendVersionProtectionState === 'storage-unavailable' ? (
              <>
                <strong>浏览器会话存储不可用</strong>
                <span>无法可靠保存版本追加的不确定结果保护。请恢复浏览器站点存储后重新载入页面。</span>
              </>
            ) : (
              <>
                <strong>请先确认上一笔版本追加结果</strong>
                <span>上一笔追加命令的响应不确定。请核对版本列表，避免重复追加同一版本。</span>
                {controller.appendVersionRetryAvailable ? (
                  <span>当前页面仅允许用未变更的同一文件和版本信息重试；新追加仍被阻断。</span>
                ) : null}
                <button
                  className="fj-review-secondary"
                  disabled={controller.appendVersionConfirmationPending}
                  onClick={() => void actions.confirmAppendVersionList()}
                  type="button"
                >
                  {controller.appendVersionConfirmationPending
                    ? '正在刷新版本列表...'
                    : '我已核对版本列表，刷新后允许继续追加'}
                </button>
              </>
            )}
          </section>
        ) : null}
        {controller.canAppendVersion &&
          (!controller.appendVersionConfirmationRequired || controller.appendVersionRetryAvailable) && (
          <CapabilityGate entryMode={props.entryMode} capability="review.version.upload">
            <AppendVersionPanel
              key={controller.nextLabel}
              nextLabel={controller.nextLabel}
              pending={mutations.appendVersion.isPending}
              progress={controller.uploadProgress}
              onSubmit={actions.appendVersion}
            />
          </CapabilityGate>
        )}
        <div className="fj-review-episode-strip-slot">
          <EpisodeStrip
            items={controller.episodeItems}
            currentItemId={data.item.reviewItemId}
            versionCounts={controller.episodeVersionCounts}
            currentLabels={controller.episodeCurrentLabels}
            onSelect={(item) => {
              if (item.reviewItemId === data.item.reviewItemId) return;
              controller.navigate(`/${props.entryMode}/projects/${props.projectRefId}/items/${item.reviewItemId}`);
            }}
          />
        </div>
      </div>
    );
  },
);
