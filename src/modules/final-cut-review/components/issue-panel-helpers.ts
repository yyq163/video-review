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
  return versions?.find((version) => version.versionId === issue.versionId) ?? {
    ...fallback,
    versionId: issue.versionId,
    label: versionLabelForIssue(fallback, issue),
  };
}
