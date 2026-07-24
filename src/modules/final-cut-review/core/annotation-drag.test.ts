import { describe, expect, it } from 'vitest';
import type { ReviewAnnotationShape } from '../contracts/types';
import {
  annotationShapeBounds,
  annotationShapeAtPoint,
  constrainAnnotationShapeWithinCanvas,
  translateAnnotationShapeWithinCanvas,
} from './annotation-drag';

const shapes: ReviewAnnotationShape[] = [
  {
    shapeId: 'pen',
    tool: 'pen',
    color: '#fff',
    lineWidth: 4,
    points: [{ x: 0.1, y: 0.1 }, { x: 0.25, y: 0.2 }],
  },
  {
    shapeId: 'arrow',
    tool: 'arrow',
    color: '#fff',
    lineWidth: 4,
    points: [{ x: 0.2, y: 0.2 }, { x: 0.4, y: 0.4 }],
  },
  {
    shapeId: 'rect',
    tool: 'rect',
    color: '#fff',
    lineWidth: 4,
    bounds: { x: 0.2, y: 0.2, width: 0.25, height: 0.2 },
  },
  {
    shapeId: 'circle',
    tool: 'circle',
    color: '#fff',
    lineWidth: 4,
    bounds: { x: 0.3, y: 0.3, width: 0.2, height: 0.2 },
  },
  {
    shapeId: 'text',
    tool: 'text',
    color: '#fff',
    lineWidth: 2,
    fontSize: 32,
    points: [{ x: 0.4, y: 0.5 }],
    text: '可移动文字',
  },
];

describe('annotation dragging geometry', () => {
  it.each(shapes)('selects and moves the whole $tool object without changing its geometry', (shape) => {
    const before = annotationShapeBounds(shape, 1280, 720);
    const hitPoint = {
      x: before.x + before.width / 2,
      y: before.y + before.height / 2,
    };
    expect(annotationShapeAtPoint([shape], hitPoint, 1280, 720)?.shapeId).toBe(shape.shapeId);

    const moved = translateAnnotationShapeWithinCanvas(shape, { x: 0.9, y: 0.9 }, 1280, 720);
    const after = annotationShapeBounds(moved, 1280, 720);
    expect(after.x).toBeGreaterThanOrEqual(0);
    expect(after.y).toBeGreaterThanOrEqual(0);
    expect(after.x + after.width).toBeLessThanOrEqual(1);
    expect(after.y + after.height).toBeLessThanOrEqual(1);
    expect(after.width).toBeCloseTo(before.width);
    expect(after.height).toBeCloseTo(before.height);
    expect(moved.tool).toBe(shape.tool);
    expect(moved.text).toBe(shape.text);
  });

  it.each(shapes)('keeps a newly created $tool object completely inside the canvas', (shape) => {
    const edgeShape: ReviewAnnotationShape = {
      ...shape,
      ...(shape.bounds
        ? { bounds: { ...shape.bounds, x: 0, y: 0, width: 1, height: 1 } }
        : {}),
      ...(shape.points
        ? {
            points: shape.points.map((point, index) => ({
              x: index === 0 ? 0 : 1,
              y: point.y < 0.3 ? 0 : 1,
            })),
          }
        : {}),
    };

    const constrained = constrainAnnotationShapeWithinCanvas(edgeShape, 1280, 720);
    const bounds = annotationShapeBounds(constrained, 1280, 720);
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.y).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(1);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(1);
  });

  it('repositions and width-constrains long edited text instead of losing its tail outside the canvas', () => {
    const longText: ReviewAnnotationShape = {
      ...shapes[4],
      fontSize: 64,
      points: [{ x: 0.98, y: 0.04 }],
      text: '这是一条会超过旧版二百六十像素命中估算的长文字批注'.repeat(20),
    };

    const constrained = constrainAnnotationShapeWithinCanvas(longText, 1280, 720);
    const bounds = annotationShapeBounds(constrained, 1280, 720);
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.y).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(1);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(1);
    expect(annotationShapeAtPoint(
      [constrained],
      { x: bounds.x + bounds.width * 0.95, y: bounds.y + bounds.height / 2 },
      1280,
      720,
    )?.shapeId).toBe(longText.shapeId);
  });

  it('keeps the rendered head and halo of a one-pixel arrow inside the canvas edge', () => {
    const canvasWidth = 1280;
    const canvasHeight = 720;
    const arrow: ReviewAnnotationShape = {
      shapeId: 'thin-edge-arrow',
      tool: 'arrow',
      color: '#fff',
      lineWidth: 1,
      points: [{ x: 0.1, y: 0 }, { x: 0.9, y: 0 }],
    };

    const constrained = constrainAnnotationShapeWithinCanvas(arrow, canvasWidth, canvasHeight);
    const [start, end] = constrained.points!.map((point) => ({
      x: point.x * canvasWidth,
      y: point.y * canvasHeight,
    }));
    const angle = Math.atan2(end.y - start.y, end.x - start.x);
    const arrowStrokeWidth = Math.max(3, arrow.lineWidth * 1.2);
    const haloStrokeWidth = arrowStrokeWidth + 5;
    const size = Math.max(18, arrowStrokeWidth * 4.8);
    const head = [
      {
        x: end.x - size * Math.cos(angle - Math.PI / 6),
        y: end.y - size * Math.sin(angle - Math.PI / 6),
      },
      {
        x: end.x - size * Math.cos(angle + Math.PI / 6),
        y: end.y - size * Math.sin(angle + Math.PI / 6),
      },
    ];
    const renderedXs = [start.x, end.x, ...head.map((point) => point.x)];
    const renderedYs = [start.y, end.y, ...head.map((point) => point.y)];
    const haloPadding = haloStrokeWidth / 2;

    expect(Math.min(...renderedXs) - haloPadding).toBeGreaterThanOrEqual(0);
    expect(Math.max(...renderedXs) + haloPadding).toBeLessThanOrEqual(canvasWidth);
    expect(Math.min(...renderedYs) - haloPadding).toBeGreaterThanOrEqual(0);
    expect(Math.max(...renderedYs) + haloPadding).toBeLessThanOrEqual(canvasHeight);
  });

  it('does not select empty space inside a diagonal pen or arrow bounding box', () => {
    const diagonalPen: ReviewAnnotationShape = {
      shapeId: 'diagonal-pen',
      tool: 'pen',
      color: '#fff',
      lineWidth: 4,
      points: [{ x: 0.1, y: 0.1 }, { x: 0.5, y: 0.5 }, { x: 0.9, y: 0.9 }],
    };
    const diagonalArrow: ReviewAnnotationShape = {
      ...diagonalPen,
      shapeId: 'diagonal-arrow',
      tool: 'arrow',
      points: [{ x: 0.1, y: 0.1 }, { x: 0.9, y: 0.9 }],
    };
    const emptyPoint = { x: 0.18, y: 0.8 };

    expect(annotationShapeAtPoint([diagonalPen], emptyPoint, 1280, 720)).toBeNull();
    expect(annotationShapeAtPoint([diagonalArrow], emptyPoint, 1280, 720)).toBeNull();
    expect(annotationShapeAtPoint(
      [diagonalArrow],
      { x: 0.5, y: 0.5 },
      1280,
      720,
    )?.shapeId).toBe(diagonalArrow.shapeId);
  });

  it('selects the visible arrow head segments as part of the whole arrow', () => {
    const arrow: ReviewAnnotationShape = {
      shapeId: 'arrow-with-head-hit',
      tool: 'arrow',
      color: '#fff',
      lineWidth: 4,
      points: [{ x: 0.1, y: 0.5 }, { x: 0.9, y: 0.5 }],
    };
    const headSize = Math.max(18, Math.max(3, arrow.lineWidth * 1.2) * 4.8);
    const renderedHeadEndpoint = {
      x: (0.9 * 1280 - headSize * Math.cos(-Math.PI / 6)) / 1280,
      y: (0.5 * 720 - headSize * Math.sin(-Math.PI / 6)) / 720,
    };

    expect(annotationShapeAtPoint(
      [arrow],
      renderedHeadEndpoint,
      1280,
      720,
    )?.shapeId).toBe(arrow.shapeId);
  });
});
