import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import type { EntryMode, ReviewItem, UploadProgress } from '../contracts/types';
import { entryLinksFor } from '../entry/entry-links';
import { useProjectDetail, useReviewMutations } from '../entry/use-review-queries';
import { AppShell, CapabilityGate, ErrorView, LoadingBlock, StatusBadge, actionError } from '../components/shared';
import { CreateItemUploadPanel } from '../components/UploadPanel';
import { ProjectMetadataEditor, type ReviewItemMetadataValues } from '../components/MetadataEditors';
import { uploadSchema, type ProjectFormValues } from '../components/ProjectForms';
import type { ReviewItemWithMetadata } from '../ports';
import { groupReviewItemsByEpisode } from '../core/episode-dedupe';
import { clearV1ListConfirmationRequired, getV1ListProtectionState } from '../adapters/http-review-uploads';
import { ProjectDetailItemList, type ProjectDetailMetadataEpisodeGroup } from './project-detail-item-list';

export function ProjectDetailPage(props: { entryMode: EntryMode }) {
  const navigate = useNavigate();
  const { projectRefId = '' } = useParams();
  const detail = useProjectDetail(props.entryMode, projectRefId);
  const mutations = useReviewMutations(props.entryMode);
  const [projectActionError, setProjectActionError] = useState<string | null>(null);
  const [projectActionMessage, setProjectActionMessage] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | undefined>(undefined);
  const [v1ListProtectionState, setV1ListProtectionState] = useState(() => props.entryMode === 'edit' ? getV1ListProtectionState(projectRefId) : 'clear');
  const v1ListConfirmationRequired = v1ListProtectionState !== 'clear';
  const [v1ListConfirmationPending, setV1ListConfirmationPending] = useState(false);
  if (detail.isLoading) {
    return (
      <AppShell entryMode={props.entryMode} homeHref={`/${props.entryMode}/projects`} entryLinks={entryLinksFor(props.entryMode)}>
        <LoadingBlock />
      </AppShell>
    );
  }

  if (!detail.data) {
    return (
      <AppShell entryMode={props.entryMode} homeHref={`/${props.entryMode}/projects`} entryLinks={entryLinksFor(props.entryMode)}>
        <ErrorView error={detail.error ?? new Error('项目不存在')} />
      </AppShell>
    );
  }

  const { project, items, versionsByItem, issuesByVersion, finalizations } = detail.data;
  const episodeGroups = groupReviewItemsByEpisode(items, { versionsByItem }) as ProjectDetailMetadataEpisodeGroup[];
  const isArchived = project.status === 'archived';
  const projectActionPending =
    mutations.updateProject.isPending ||
    mutations.archiveProject.isPending ||
    mutations.restoreProject.isPending ||
    mutations.deleteProject.isPending;
  const itemActionPending = mutations.updateReviewItem.isPending || mutations.deleteReviewItem.isPending;

  const updateProjectMetadata = async (values: ProjectFormValues) => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    await mutations.updateProject.mutateAsync({ projectRefId, ...values });
    setProjectActionMessage('项目资料已更新。');
  };

  const updateReviewItemMetadata = async (item: ReviewItemWithMetadata, values: ReviewItemMetadataValues) => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    await mutations.updateReviewItem.mutateAsync({ projectRefId, reviewItemId: item.reviewItemId, ...values });
    setProjectActionMessage(`成片「${values.title}」元数据已更新。`);
  };

  const archiveProject = async () => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    try {
      await mutations.archiveProject.mutateAsync({ projectRefId });
      setProjectActionMessage('项目已归档，现有成片、版本、意见、定稿和文件保持可读。');
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const restoreProject = async () => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    try {
      await mutations.restoreProject.mutateAsync({ projectRefId });
      setProjectActionMessage('项目已恢复，可继续剪辑管理。');
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const deleteProject = async () => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    const confirmed = window.confirm(`确认删除项目「${project.name}」？删除后项目会从审阅列表移除，历史记录和媒体文件仍保留。`);
    if (!confirmed) return;
    try {
      await mutations.deleteProject.mutateAsync({ projectRefId, confirmed: true });
      navigate(`/${props.entryMode}/projects`);
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const deleteReviewItem = async (item: ReviewItem) => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    const confirmed = window.confirm(
      `确认删除分集「${item.title}」？该操作仅用于审核开始前去重，会永久删除该分集、唯一未审核版本和可安全释放的原始媒体文件；待清理分片将由维护任务回收，且无法撤销。`,
    );
    if (!confirmed) return;
    try {
      await mutations.deleteReviewItem.mutateAsync({ projectRefId, reviewItemId: item.reviewItemId, confirmed: true });
      setProjectActionMessage('分集已删除，列表已刷新。');
      await detail.refetch();
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const confirmV1List = async () => {
    setProjectActionError(null);
    setV1ListConfirmationPending(true);
    try {
      await detail.refetch({ throwOnError: true });
      clearV1ListConfirmationRequired(projectRefId);
      const nextProtectionState = getV1ListProtectionState(projectRefId);
      setV1ListProtectionState(nextProtectionState);
      if (nextProtectionState === 'storage-unavailable') {
        throw new Error('浏览器会话存储不可用，无法安全解除 V1 创建保护。');
      }
      setProjectActionMessage('已确认当前成片列表，可以继续创建新的 V1。');
    } catch (caught) {
      setProjectActionError(`列表刷新失败，尚未解除 V1 创建保护：${actionError(caught)}`);
    } finally {
      setV1ListConfirmationPending(false);
    }
  };

  return (
    <AppShell
      entryMode={props.entryMode}
      homeHref={`/${props.entryMode}/projects`}
      entryLinks={entryLinksFor(props.entryMode)}
      right={
        <>
          {isArchived ? (
            <CapabilityGate entryMode={props.entryMode} capability="review.project.restore">
              <button className="fj-review-secondary" disabled={projectActionPending} onClick={restoreProject} type="button">
                恢复项目
              </button>
            </CapabilityGate>
          ) : (
            <CapabilityGate entryMode={props.entryMode} capability="review.project.archive">
              <button className="fj-review-secondary" disabled={projectActionPending} onClick={archiveProject} type="button">
                归档项目
              </button>
            </CapabilityGate>
          )}
          {!isArchived ? (
            <CapabilityGate entryMode={props.entryMode} capability="review.project.delete">
              <button className="fj-review-secondary is-danger" disabled={projectActionPending} onClick={deleteProject} type="button">
                删除项目
              </button>
            </CapabilityGate>
          ) : null}
          <Link className="fj-review-secondary" to={`/${props.entryMode}/projects`}>
            返回项目
          </Link>
        </>
      }
    >
      <section className="fj-review-page fj-review-project-detail">
        <div className="fj-review-page-heading">
          <div>
            <span>{project.code}</span>
            <div className="fj-review-icon-text">
              <h1>
                {project.name}
                <StatusBadge status={project.status} />
              </h1>
              {!isArchived ? (
                <CapabilityGate entryMode={props.entryMode} capability="review.project.update">
                  <ProjectMetadataEditor project={project} pending={mutations.updateProject.isPending} onSubmit={updateProjectMetadata} />
                </CapabilityGate>
              ) : null}
            </div>
          </div>
          <p>{project.description || '暂无项目说明'}</p>
        </div>
        {projectActionMessage && <div className="fj-review-notice">{projectActionMessage}</div>}
        {projectActionError && <div className="fj-review-error">{projectActionError}</div>}
        {v1ListConfirmationRequired ? (
          <section className="fj-review-readonly-notice" data-testid="v1-list-confirmation-required" role="alert">
            {v1ListProtectionState === 'storage-unavailable' ? (
              <>
                <strong>浏览器会话存储不可用</strong>
                <span>无法可靠保存 V1 不确定结果保护。请恢复浏览器站点存储后重新载入页面。</span>
              </>
            ) : (
              <>
                <strong>请先确认上一笔 V1 的列表结果</strong>
                <span>上一笔 V1 命令的响应不确定。请核对下方成片列表，避免重复创建。</span>
                <button
                  className="fj-review-secondary"
                  disabled={v1ListConfirmationPending}
                  onClick={() => void confirmV1List()}
                  type="button"
                >
                  {v1ListConfirmationPending ? '正在刷新列表...' : '我已核对列表，允许新建 V1'}
                </button>
              </>
            )}
          </section>
        ) : null}
        {isArchived ? (
          <section className="fj-review-readonly-notice" data-testid="archived-readonly-notice">
            <strong>项目已归档</strong>
            <span>归档状态只允许查看既有资料；恢复项目后才能创建成片、上传版本或执行写操作。</span>
          </section>
        ) : (
          <CapabilityGate entryMode={props.entryMode} capability="review.item.create">
            <CreateItemUploadPanel
              pending={mutations.createReviewItemWithVersion.isPending || Boolean(uploadProgress)}
              blockedForListConfirmation={v1ListConfirmationRequired}
              progress={uploadProgress}
              onSubmit={async (input) => {
                const parsedInput = uploadSchema.safeParse(input);
                if (!parsedInput.success) {
                  return {
                    outcome: 'failed' as const,
                    message: parsedInput.error.issues[0]?.message ?? '成片信息校验失败。',
                  };
                }
                const validatedInput = parsedInput.data;
                setProjectActionError(null);
                setUploadProgress({ stage: 'validating', percent: 0, totalBytes: validatedInput.file.size });
                try {
                  const created = await mutations.createReviewItemWithVersion.mutateAsync({
                    projectRefId,
                    ...validatedInput,
                    onProgress: setUploadProgress,
                  });
                  setUploadProgress({
                    stage: 'completed',
                    percent: 100,
                    bytesSent: validatedInput.file.size,
                    totalBytes: validatedInput.file.size,
                  });
                  clearV1ListConfirmationRequired(projectRefId);
                  const nextProtectionState = getV1ListProtectionState(projectRefId);
                  setV1ListProtectionState(nextProtectionState);
                  const stopBatch = nextProtectionState === 'storage-unavailable';
                  if (stopBatch) {
                    setProjectActionError('浏览器会话存储不可用，后续 V1 上传已停止；已成功的文件不会重传。');
                  }
                  setUploadProgress(undefined);
                  void detail.refetch({ throwOnError: true }).then((refreshed) => {
                    const confirmedInList = refreshed.data?.items.some(
                      (item) => item.reviewItemId === created.item.reviewItemId,
                    );
                    if (!confirmedInList) {
                      setProjectActionMessage('文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。');
                    }
                  }).catch(() => {
                    setProjectActionMessage('文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。');
                  });
                  return { outcome: 'success' as const, stopBatch };
                } catch (caught) {
                  setUploadProgress(undefined);
                  const nextProtectionState = getV1ListProtectionState(projectRefId);
                  setV1ListProtectionState(nextProtectionState);
                  if (nextProtectionState !== 'clear') {
                    return {
                      outcome: 'uncertain' as const,
                      message: '上传结果不确定，请先核对待审列表；确认前不会继续本批次或重传。',
                    };
                  }
                  return { outcome: 'failed' as const, message: actionError(caught) };
                }
              }}
            />
          </CapabilityGate>
        )}
        <ProjectDetailItemList
          entryMode={props.entryMode}
          episodeGroups={episodeGroups}
          finalizations={finalizations}
          isArchived={isArchived}
          issuesByVersion={issuesByVersion}
          itemActionPending={itemActionPending}
          onDeleteReviewItem={(item) => void deleteReviewItem(item)}
          onUpdateReviewItemMetadata={updateReviewItemMetadata}
          projectRefId={projectRefId}
          versionsByItem={versionsByItem}
        />
      </section>
    </AppShell>
  );
}
