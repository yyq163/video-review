import type { AnnotationTool, ReviewAnnotationShape, ReviewIssue, ReviewVersion } from '../contracts/types';

const annotationToolLabels = {
  text: '文字',
  pen: '画笔',
  arrow: '箭头',
  rect: '矩形',
  circle: '圆形',
} satisfies Record<AnnotationTool, string>;

const annotationToolOrder: AnnotationTool[] = ['text', 'pen', 'arrow', 'rect', 'circle'];

export function summarizeAnnotationShapes(shapes: ReviewAnnotationShape[]): Array<{ tool: AnnotationTool; label: string }> {
  const counts = new Map<AnnotationTool, number>();
  shapes.forEach((shape) => {
    counts.set(shape.tool, (counts.get(shape.tool) ?? 0) + 1);
  });
  return annotationToolOrder
    .filter((tool) => counts.has(tool))
    .map((tool) => {
      const count = counts.get(tool) ?? 0;
      return {
        tool,
        label: count > 1 ? `${annotationToolLabels[tool]} ${count}` : annotationToolLabels[tool],
      };
    });
}

export function versionLabelForIssue(version: ReviewVersion, issue: ReviewIssue): string {
  if (version.versionId === issue.versionId) return version.label;
  const match = issue.versionId.match(/_v(\d+)$/i);
  return match ? `V${match[1]}` : issue.versionId;
}

export function versionForIssue(
  versions: ReviewVersion[] | undefined,
  fallback: ReviewVersion,
  issue: ReviewIssue,
): ReviewVersion {
  const matchingVersions = (versions ?? [fallback]).filter(
    (version) => version.versionId === issue.versionId,
  );
  if (matchingVersions.length !== 1) {
    throw new Error(`意见 ${issue.issueId} 无法唯一映射到真实版本 ${issue.versionId}`);
  }
  return matchingVersions[0];
}

export function sortedHistoricalIssues(
  issues: ReviewIssue[],
  versions: ReviewVersion[] | undefined,
  fallback: ReviewVersion,
): Array<{ issue: ReviewIssue; version: ReviewVersion }> {
  return issues
    .filter((issue) => !issue.deletedAt)
    .map((issue, stableIndex) => ({
      issue,
      version: versionForIssue(versions, fallback, issue),
      stableIndex,
    }))
    .sort(
      (left, right) =>
        right.version.versionNo - left.version.versionNo ||
        left.issue.timestampMs - right.issue.timestampMs ||
        left.stableIndex - right.stableIndex,
    )
    .map(({ issue, version }) => ({ issue, version }));
}
