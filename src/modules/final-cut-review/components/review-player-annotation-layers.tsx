import type { CSSProperties, ReactNode } from 'react';
import type { ReviewAnnotationSet, ReviewAnnotationShape } from '../contracts/types';
import {
  normalizedPathToCanvasPath,
  normalizedVideoPointToCanvasPoint,
} from '../core/coordinates';
import { shapeFontSize } from './review-player-annotation-utils';

function renderShape(shape: ReviewAnnotationShape, canvasWidth = 100, canvasHeight = 100): ReactNode {
  const strokeWidth = Math.max(1, shape.lineWidth) * 0.45;
  if (shape.tool === 'pen' && shape.points?.length) {
    const path = normalizedPathToCanvasPath({ points: shape.points, canvasWidth, canvasHeight });
    const d = path.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' ');
    return (
      <path
        d={d}
        fill="none"
        stroke={shape.color}
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={strokeWidth}
      />
    );
  }
  const points = shape.points;
  if (shape.tool === 'arrow' && points && points.length >= 2) {
    const [start, end] = normalizedPathToCanvasPath({
      points: [points[0], points[points.length - 1]],
      canvasWidth,
      canvasHeight,
    });
    const angle = Math.atan2(end.y - start.y, end.x - start.x);
    const arrowStrokeWidth = Math.max(3, Math.max(1, shape.lineWidth) * 1.2);
    const haloStrokeWidth = arrowStrokeWidth + 5;
    const size = Math.max(18, arrowStrokeWidth * 4.8);
    const left = {
      x: end.x - size * Math.cos(angle - Math.PI / 6),
      y: end.y - size * Math.sin(angle - Math.PI / 6),
    };
    const right = {
      x: end.x - size * Math.cos(angle + Math.PI / 6),
      y: end.y - size * Math.sin(angle + Math.PI / 6),
    };
    const headPath = `M ${left.x} ${left.y} L ${end.x} ${end.y} L ${right.x} ${right.y}`;
    return (
      <>
        <line
          x1={start.x}
          y1={start.y}
          x2={end.x}
          y2={end.y}
          stroke="rgba(0,0,0,0.72)"
          strokeLinecap="round"
          strokeWidth={haloStrokeWidth}
        />
        <path
          d={headPath}
          fill="none"
          stroke="rgba(0,0,0,0.72)"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={haloStrokeWidth}
        />
        <line
          x1={start.x}
          y1={start.y}
          x2={end.x}
          y2={end.y}
          stroke={shape.color}
          strokeLinecap="round"
          strokeWidth={arrowStrokeWidth}
        />
        <path
          d={headPath}
          fill="none"
          stroke={shape.color}
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={arrowStrokeWidth}
        />
      </>
    );
  }
  if (shape.bounds) {
    const x = shape.bounds.x * canvasWidth;
    const y = shape.bounds.y * canvasHeight;
    const width = shape.bounds.width * canvasWidth;
    const height = shape.bounds.height * canvasHeight;
    if (shape.tool === 'circle') {
      return (
        <ellipse
          cx={x + width / 2}
          cy={y + height / 2}
          rx={Math.max(1, width / 2)}
          ry={Math.max(1, height / 2)}
          fill="transparent"
          stroke={shape.color}
          strokeWidth={strokeWidth}
        />
      );
    }
    return (
      <>
        <rect
          x={x}
          y={y}
          width={width}
          height={height}
          rx="3"
          fill="transparent"
          stroke={shape.color}
          strokeWidth={strokeWidth}
        />
        {shape.text && (
          <text
            x={x}
            y={Math.max(shapeFontSize(shape), y - 6)}
            fill={shape.color}
            fontSize={shapeFontSize(shape)}
            fontWeight="700"
            paintOrder="stroke"
            stroke="rgba(0,0,0,0.58)"
            strokeWidth={Math.max(2, shapeFontSize(shape) * 0.08)}
          >
            {shape.text}
          </text>
        )}
      </>
    );
  }
  if (shape.tool === 'text' && shape.points?.[0] && shape.text) {
    const point = normalizedVideoPointToCanvasPoint({
      point: shape.points[0],
      canvasWidth,
      canvasHeight,
    });
    return (
      <text
        x={point.x}
        y={point.y}
        fill={shape.color}
        fontSize={shapeFontSize(shape)}
        fontWeight="700"
        paintOrder="stroke"
        stroke="rgba(0,0,0,0.58)"
        strokeWidth={Math.max(2, shapeFontSize(shape) * 0.08)}
      >
        {shape.text}
      </text>
    );
  }
  return null;
}

export function SavedAnnotationLayer(props: {
  annotationSet: ReviewAnnotationSet | null;
  selectedIssueId?: string;
  canvasWidth: number;
  canvasHeight: number;
  layerStyle: CSSProperties;
}) {
  return (
    <svg
      className="fj-review-annotation-layer fj-review-saved-layer"
      data-testid="saved-annotation-layer"
      data-annotation-set-id={props.annotationSet?.annotationSetId ?? ''}
      data-selected-issue-id={props.selectedIssueId ?? ''}
      viewBox={`0 0 ${props.canvasWidth} ${props.canvasHeight}`}
      preserveAspectRatio="none"
      style={props.layerStyle}
    >
      {props.annotationSet?.shapes.map((shape) => (
        <g key={shape.shapeId}>{renderShape(shape, props.canvasWidth, props.canvasHeight)}</g>
      ))}
    </svg>
  );
}

export function DraftAnnotationLayer(props: {
  draftShapes: ReviewAnnotationShape[];
  activeShape: ReviewAnnotationShape | null;
  canvasWidth: number;
  canvasHeight: number;
  layerStyle: CSSProperties;
}) {
  const activeShape =
    props.activeShape && !props.draftShapes.some((shape) => shape.shapeId === props.activeShape?.shapeId)
      ? props.activeShape
      : null;
  return (
    <svg
      className="fj-review-annotation-layer fj-review-draft-layer"
      data-testid="draft-annotation-layer"
      viewBox={`0 0 ${props.canvasWidth} ${props.canvasHeight}`}
      preserveAspectRatio="none"
      style={props.layerStyle}
    >
      {props.draftShapes.map((shape) => (
        <g key={shape.shapeId}>{renderShape(shape, props.canvasWidth, props.canvasHeight)}</g>
      ))}
      {activeShape ? (
        <g key={activeShape.shapeId}>{renderShape(activeShape, props.canvasWidth, props.canvasHeight)}</g>
      ) : null}
    </svg>
  );
}
