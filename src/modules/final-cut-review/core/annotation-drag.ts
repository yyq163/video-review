import type {
  NormalizedBounds,
  NormalizedPoint,
  ReviewAnnotationShape,
} from '../contracts/types';

const MIN_HIT_TARGET_PX = 12;
const NORMALIZED_BOUNDARY_EPSILON = 1e-12;

export function annotationArrowRenderMetricsPx(lineWidth: number) {
  const arrowStrokeWidth = Math.max(3, Math.max(1, finite(lineWidth, 1)) * 1.2);
  const haloStrokeWidth = arrowStrokeWidth + 5;
  return {
    arrowStrokeWidth,
    haloStrokeWidth,
    headSize: Math.max(18, arrowStrokeWidth * 4.8),
  };
}

function finite(value: number, fallback = 0): number {
  return Number.isFinite(value) ? value : fallback;
}

function dimensions(canvasWidth: number, canvasHeight: number) {
  return {
    width: Math.max(1, finite(canvasWidth, 1)),
    height: Math.max(1, finite(canvasHeight, 1)),
  };
}

function textMetrics(
  shape: ReviewAnnotationShape,
  canvasWidth: number,
) {
  const fontSize = Math.max(14, finite(shape.fontSize ?? 32, 32));
  const strokeWidth = Math.max(2, fontSize * 0.08);
  const textLength = Math.max(2, (shape.text ?? '文字批注').length);
  const naturalWidth = Math.max(56, fontSize * textLength * 0.72);
  return {
    fontSize,
    strokeWidth,
    width: Math.max(1, Math.min(Math.max(1, canvasWidth - strokeWidth * 2), naturalWidth)),
  };
}

export function annotationTextRenderWidthPx(
  shape: ReviewAnnotationShape,
  canvasWidth: number,
): number {
  return textMetrics(shape, Math.max(1, finite(canvasWidth, 1))).width;
}

function textBounds(
  shape: ReviewAnnotationShape,
  canvasWidth: number,
  canvasHeight: number,
): NormalizedBounds {
  const point = shape.points?.[0] ?? { x: 0, y: 0 };
  const metrics = textMetrics(shape, canvasWidth);
  const heightPx = Math.min(canvasHeight, Math.max(32, metrics.fontSize * 1.35));
  return {
    x: finite(point.x) - metrics.strokeWidth / canvasWidth,
    y: finite(point.y) - heightPx / canvasHeight,
    width: (metrics.width + metrics.strokeWidth * 2) / canvasWidth,
    height: heightPx / canvasHeight,
  };
}

function pointBounds(
  shape: ReviewAnnotationShape,
  canvasWidth: number,
  canvasHeight: number,
): NormalizedBounds {
  const points = shape.points?.length ? shape.points : [{ x: 0, y: 0 }];
  if (shape.tool === 'arrow' && points.length >= 2) {
    const start = {
      x: finite(points[0].x) * canvasWidth,
      y: finite(points[0].y) * canvasHeight,
    };
    const lastPoint = points[points.length - 1];
    const end = {
      x: finite(lastPoint.x) * canvasWidth,
      y: finite(lastPoint.y) * canvasHeight,
    };
    const angle = Math.atan2(end.y - start.y, end.x - start.x);
    const metrics = annotationArrowRenderMetricsPx(shape.lineWidth);
    const head = [
      {
        x: end.x - metrics.headSize * Math.cos(angle - Math.PI / 6),
        y: end.y - metrics.headSize * Math.sin(angle - Math.PI / 6),
      },
      {
        x: end.x - metrics.headSize * Math.cos(angle + Math.PI / 6),
        y: end.y - metrics.headSize * Math.sin(angle + Math.PI / 6),
      },
    ];
    const haloPadding = metrics.haloStrokeWidth / 2;
    const xs = [start.x, end.x, ...head.map((point) => point.x)];
    const ys = [start.y, end.y, ...head.map((point) => point.y)];
    const minX = Math.min(...xs) - haloPadding;
    const maxX = Math.max(...xs) + haloPadding;
    const minY = Math.min(...ys) - haloPadding;
    const maxY = Math.max(...ys) + haloPadding;
    return {
      x: minX / canvasWidth,
      y: minY / canvasHeight,
      width: Math.max(0, maxX - minX) / canvasWidth,
      height: Math.max(0, maxY - minY) / canvasHeight,
    };
  }
  const xs = points.map((point) => finite(point.x));
  const ys = points.map((point) => finite(point.y));
  const strokePaddingPx = Math.max(
    MIN_HIT_TARGET_PX / 2,
    finite(shape.lineWidth, 1) * (shape.tool === 'arrow' ? 5 : 1.5),
  );
  const paddingX = strokePaddingPx / canvasWidth;
  const paddingY = strokePaddingPx / canvasHeight;
  const minX = Math.min(...xs) - paddingX;
  const maxX = Math.max(...xs) + paddingX;
  const minY = Math.min(...ys) - paddingY;
  const maxY = Math.max(...ys) + paddingY;
  return {
    x: minX,
    y: minY,
    width: Math.max(0, maxX - minX),
    height: Math.max(0, maxY - minY),
  };
}

export function annotationShapeBounds(
  shape: ReviewAnnotationShape,
  canvasWidth: number,
  canvasHeight: number,
): NormalizedBounds {
  const canvas = dimensions(canvasWidth, canvasHeight);
  if (shape.tool === 'text') return textBounds(shape, canvas.width, canvas.height);
  if (shape.bounds) {
    const paddingX = Math.max(1, finite(shape.lineWidth, 1)) / canvas.width;
    const paddingY = Math.max(1, finite(shape.lineWidth, 1)) / canvas.height;
    return {
      x: finite(shape.bounds.x) - paddingX,
      y: finite(shape.bounds.y) - paddingY,
      width: Math.max(0, finite(shape.bounds.width)) + paddingX * 2,
      height: Math.max(0, finite(shape.bounds.height)) + paddingY * 2,
    };
  }
  return pointBounds(shape, canvas.width, canvas.height);
}

function clampDelta(delta: number, min: number, max: number): number {
  if (min > max) return (min + max) / 2;
  return Math.min(max, Math.max(min, finite(delta)));
}

function translatePoint(point: NormalizedPoint, deltaX: number, deltaY: number): NormalizedPoint {
  return {
    x: finite(point.x) + deltaX,
    y: finite(point.y) + deltaY,
  };
}

export function constrainAnnotationShapeWithinCanvas(
  shape: ReviewAnnotationShape,
  canvasWidth: number,
  canvasHeight: number,
): ReviewAnnotationShape {
  const canvas = dimensions(canvasWidth, canvasHeight);
  if (shape.bounds) {
    const paddingX = Math.max(1, finite(shape.lineWidth, 1)) / canvas.width;
    const paddingY = Math.max(1, finite(shape.lineWidth, 1)) / canvas.height;
    const width = Math.min(
      Math.max(0, finite(shape.bounds.width)),
      Math.max(0, 1 - paddingX * 2),
    );
    const height = Math.min(
      Math.max(0, finite(shape.bounds.height)),
      Math.max(0, 1 - paddingY * 2),
    );
    return {
      ...shape,
      bounds: {
        ...shape.bounds,
        x: Math.max(
          paddingX,
          Math.min(1 - paddingX - width, finite(shape.bounds.x)),
        ),
        y: Math.max(
          paddingY,
          Math.min(1 - paddingY - height, finite(shape.bounds.y)),
        ),
        width,
        height,
      },
    };
  }
  if (shape.tool !== 'text' && shape.points?.length) {
    const arrowMetrics = shape.tool === 'arrow'
      ? annotationArrowRenderMetricsPx(shape.lineWidth)
      : null;
    const strokePaddingPx = Math.max(
      MIN_HIT_TARGET_PX / 2,
      arrowMetrics
        ? arrowMetrics.headSize + arrowMetrics.haloStrokeWidth / 2
        : finite(shape.lineWidth, 1) * 1.5,
    );
    const paddingX = Math.min(0.5, strokePaddingPx / canvas.width);
    const paddingY = Math.min(0.5, strokePaddingPx / canvas.height);
    const xs = shape.points.map((point) => finite(point.x));
    const ys = shape.points.map((point) => finite(point.y));
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const availableWidth = Math.max(0, 1 - paddingX * 2);
    const availableHeight = Math.max(0, 1 - paddingY * 2);
    const spanX = Math.max(0, maxX - minX);
    const spanY = Math.max(0, maxY - minY);
    const scaleX = spanX > availableWidth && spanX > 0 ? availableWidth / spanX : 1;
    const scaleY = spanY > availableHeight && spanY > 0 ? availableHeight / spanY : 1;
    const points = shape.points.map((point) => ({
      x: paddingX + (finite(point.x) - minX) * scaleX,
      y: paddingY + (finite(point.y) - minY) * scaleY,
    }));
    const scaled: ReviewAnnotationShape = { ...shape, points };
    return translateAnnotationShapeWithinCanvas(scaled, {
      x: minX - paddingX,
      y: minY - paddingY,
    }, canvas.width, canvas.height);
  }
  return translateAnnotationShapeWithinCanvas(
    shape,
    { x: 0, y: 0 },
    canvas.width,
    canvas.height,
  );
}

export function translateAnnotationShapeWithinCanvas(
  shape: ReviewAnnotationShape,
  delta: NormalizedPoint,
  canvasWidth: number,
  canvasHeight: number,
): ReviewAnnotationShape {
  const constrained = constrainOversizedShape(shape, canvasWidth, canvasHeight);
  const bounds = annotationShapeBounds(constrained, canvasWidth, canvasHeight);
  const epsilonX = bounds.width < 1 ? NORMALIZED_BOUNDARY_EPSILON : 0;
  const epsilonY = bounds.height < 1 ? NORMALIZED_BOUNDARY_EPSILON : 0;
  const deltaX = clampDelta(
    delta.x,
    -bounds.x + epsilonX,
    1 - bounds.x - bounds.width - epsilonX,
  );
  const deltaY = clampDelta(
    delta.y,
    -bounds.y + epsilonY,
    1 - bounds.y - bounds.height - epsilonY,
  );
  return {
    ...constrained,
    ...(constrained.bounds
      ? { bounds: {
          ...constrained.bounds,
          x: finite(constrained.bounds.x) + deltaX,
          y: finite(constrained.bounds.y) + deltaY,
        } }
      : {}),
    ...(constrained.points
      ? { points: constrained.points.map((point) => translatePoint(point, deltaX, deltaY)) }
      : {}),
  };
}

function constrainOversizedShape(
  shape: ReviewAnnotationShape,
  canvasWidth: number,
  canvasHeight: number,
): ReviewAnnotationShape {
  const bounds = annotationShapeBounds(shape, canvasWidth, canvasHeight);
  if (bounds.width <= 1 && bounds.height <= 1) return shape;
  if (shape.bounds) {
    const canvas = dimensions(canvasWidth, canvasHeight);
    const paddingX = Math.max(1, finite(shape.lineWidth, 1)) / canvas.width;
    const paddingY = Math.max(1, finite(shape.lineWidth, 1)) / canvas.height;
    return {
      ...shape,
      bounds: {
        ...shape.bounds,
        width: Math.min(finite(shape.bounds.width), Math.max(0, 1 - paddingX * 2)),
        height: Math.min(finite(shape.bounds.height), Math.max(0, 1 - paddingY * 2)),
      },
    };
  }
  return shape;
}

export function annotationShapeAtPoint(
  shapes: ReviewAnnotationShape[],
  point: NormalizedPoint,
  canvasWidth: number,
  canvasHeight: number,
  hitTargetWidth = canvasWidth,
  hitTargetHeight = canvasHeight,
): ReviewAnnotationShape | null {
  const canvas = dimensions(canvasWidth, canvasHeight);
  const hitTarget = dimensions(hitTargetWidth, hitTargetHeight);
  const paddingX = MIN_HIT_TARGET_PX / hitTarget.width;
  const paddingY = MIN_HIT_TARGET_PX / hitTarget.height;
  const pointToSegmentDistance = (
    candidate: NormalizedPoint,
    start: NormalizedPoint,
    end: NormalizedPoint,
  ) => {
    const candidateX = candidate.x * hitTarget.width;
    const candidateY = candidate.y * hitTarget.height;
    const startX = start.x * hitTarget.width;
    const startY = start.y * hitTarget.height;
    const endX = end.x * hitTarget.width;
    const endY = end.y * hitTarget.height;
    const deltaX = endX - startX;
    const deltaY = endY - startY;
    const lengthSquared = deltaX * deltaX + deltaY * deltaY;
    const projection = lengthSquared === 0
      ? 0
      : Math.min(
        1,
        Math.max(
          0,
          ((candidateX - startX) * deltaX + (candidateY - startY) * deltaY) /
            lengthSquared,
        ),
      );
    return Math.hypot(
      candidateX - (startX + projection * deltaX),
      candidateY - (startY + projection * deltaY),
    );
  };
  for (let index = shapes.length - 1; index >= 0; index -= 1) {
    const shape = shapes[index];
    const bounds = annotationShapeBounds(shape, canvas.width, canvas.height);
    const insideBounds =
      point.x >= bounds.x - paddingX &&
      point.x <= bounds.x + bounds.width + paddingX &&
      point.y >= bounds.y - paddingY &&
      point.y <= bounds.y + bounds.height + paddingY;
    if (!insideBounds) continue;
    if ((shape.tool === 'pen' || shape.tool === 'arrow') && shape.points?.length) {
      const pathSegments: Array<[NormalizedPoint, NormalizedPoint]> = shape.points.length === 1
        ? [[shape.points[0], shape.points[0]]]
        : shape.points.slice(1).map((end, pointIndex) => [shape.points![pointIndex], end]);
      let hitRadius = Math.max(MIN_HIT_TARGET_PX / 2, finite(shape.lineWidth, 1) / 2);
      if (shape.tool === 'arrow' && shape.points.length >= 2) {
        const start = shape.points[0];
        const end = shape.points[shape.points.length - 1];
        const angle = Math.atan2(
          (end.y - start.y) * hitTarget.height,
          (end.x - start.x) * hitTarget.width,
        );
        const metrics = annotationArrowRenderMetricsPx(shape.lineWidth);
        const left = {
          x: end.x - metrics.headSize * Math.cos(angle - Math.PI / 6) / hitTarget.width,
          y: end.y - metrics.headSize * Math.sin(angle - Math.PI / 6) / hitTarget.height,
        };
        const right = {
          x: end.x - metrics.headSize * Math.cos(angle + Math.PI / 6) / hitTarget.width,
          y: end.y - metrics.headSize * Math.sin(angle + Math.PI / 6) / hitTarget.height,
        };
        pathSegments.push([left, end], [end, right]);
        hitRadius = Math.max(hitRadius, metrics.haloStrokeWidth / 2);
      }
      const distances = pathSegments.map(([start, end]) =>
        pointToSegmentDistance(point, start, end));
      if (distances.some((distance) => distance <= hitRadius)) return shape;
      continue;
    }
    return shape;
  }
  return null;
}
