import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent,
  type RefObject,
} from 'react';
import type { NormalizedPoint, ReviewAnnotationShape } from '../contracts/types';
import {
  clampNormalizedPoint,
  computeContainedVideoRect,
  boundsFromPoints,
  pointerToNormalizedVideoPoint,
} from '../core/coordinates';
import {
  annotationShapeAtPoint,
  constrainAnnotationShapeWithinCanvas,
  translateAnnotationShapeWithinCanvas,
} from '../core/annotation-drag';
import { createUuid } from '../core/uuid';
import {
  clampTextFontSize,
  DEFAULT_TEXT_FONT_SIZE,
  shapeFontSize,
  shapeLabel,
} from './review-player-annotation-utils';
import type { AnnotationEditorTool, RectLike } from './review-player-types';

interface AnnotationOptions {
  stageRef: RefObject<HTMLDivElement | null>;
  videoRef: RefObject<HTMLVideoElement | null>;
  scopeKey: string;
  displayVideoWidth: number;
  displayVideoHeight: number;
  displayMaxScale: number;
  readonly: boolean;
  onDraftChange(shapes: ReviewAnnotationShape[]): void;
  onPause(): void;
}

export function useReviewPlayerAnnotations(options: AnnotationOptions) {
  const { onDraftChange } = options;
  const [tool, setToolState] = useState<AnnotationEditorTool>('select');
  const [color, setColor] = useState('#57e3d2');
  const [lineWidth, setLineWidth] = useState(3);
  const [fontSize, setFontSize] = useState(DEFAULT_TEXT_FONT_SIZE);
  const [draftShapes, setDraftShapes] = useState<ReviewAnnotationShape[]>([]);
  const [redoShapes, setRedoShapes] = useState<ReviewAnnotationShape[]>([]);
  const drawingRef = useRef<{ start: NormalizedPoint; points: NormalizedPoint[]; shapeId: string } | null>(null);
  const draggingRef = useRef<{
    moved: boolean;
    origin: NormalizedPoint;
    pointerId: number;
    shape: ReviewAnnotationShape;
    toolAtStart: AnnotationEditorTool;
  } | null>(null);
  const [activeShape, setActiveShape] = useState<ReviewAnnotationShape | null>(null);
  const activeShapeRef = useRef<ReviewAnnotationShape | null>(null);
  const [selectedShapeId, setSelectedShapeId] = useState<string | null>(null);
  const [activeTextEditor, setActiveTextEditor] = useState<{ scopeKey: string; shapeId: string } | null>(null);
  const activeScopeRef = useRef(options.scopeKey);
  const textInputRef = useRef<HTMLInputElement | null>(null);
  const activeTextShapeId = activeTextEditor?.scopeKey === options.scopeKey ? activeTextEditor.shapeId : null;

  const closeTextEditor = useCallback(() => {
    setActiveTextEditor(null);
  }, []);

  useEffect(() => {
    if (activeScopeRef.current === options.scopeKey) return;
    activeScopeRef.current = options.scopeKey;
    const frame = window.requestAnimationFrame(closeTextEditor);
    return () => window.cancelAnimationFrame(frame);
  }, [closeTextEditor, options.scopeKey]);

  const setTool = useCallback(
    (nextTool: AnnotationEditorTool) => {
      setToolState(nextTool);
      if (nextTool !== 'text') closeTextEditor();
    },
    [closeTextEditor],
  );

  const clearDraft = useCallback(() => {
    setDraftShapes([]);
    setRedoShapes([]);
    setActiveShape(null);
    setSelectedShapeId(null);
    closeTextEditor();
    activeShapeRef.current = null;
    drawingRef.current = null;
    draggingRef.current = null;
    onDraftChange([]);
  }, [closeTextEditor, onDraftChange]);

  useEffect(() => {
    onDraftChange(draftShapes);
  }, [draftShapes, onDraftChange]);

  useEffect(() => {
    if (!activeTextShapeId) return;
    textInputRef.current?.focus();
    textInputRef.current?.select();
  }, [activeTextShapeId]);

  useEffect(() => {
    if (!options.readonly) return;
    const frame = window.requestAnimationFrame(() => {
      clearDraft();
      setTool('select');
    });
    return () => window.cancelAnimationFrame(frame);
  }, [clearDraft, options.readonly, setTool]);

  const makeShape = useCallback(
    (start: NormalizedPoint, current: NormalizedPoint, points: NormalizedPoint[], shapeId: string) => {
      if (tool === 'select') return null;
      if (tool === 'text') {
        return { shapeId, tool, color, lineWidth, fontSize, points: [start], text: '文字批注' };
      }
      if (tool === 'pen') return { shapeId, tool, color, lineWidth, points };
      if (tool === 'arrow') {
        return {
          shapeId,
          tool,
          color,
          lineWidth,
          points: [start, current],
          text: shapeLabel({ shapeId, tool, color, lineWidth }),
        };
      }
      return { shapeId, tool, color, lineWidth, bounds: boundsFromPoints(start, current) };
    },
    [color, fontSize, lineWidth, tool],
  );

  const pointFromEvent = (
    event: PointerEvent<HTMLDivElement>,
    clampOutside = false,
  ): NormalizedPoint | null => {
    const rect = options.stageRef.current?.getBoundingClientRect();
    if (!rect) return null;
    const point = pointerToNormalizedVideoPoint({
      clientX: event.clientX,
      clientY: event.clientY,
      containerRect: rect as RectLike,
      videoWidth: options.displayVideoWidth,
      videoHeight: options.displayVideoHeight,
      maxScale: options.displayMaxScale,
    });
    if (point || !clampOutside) return point;
    const videoRect = computeContainedVideoRect({
      containerWidth: rect.width,
      containerHeight: rect.height,
      videoWidth: options.displayVideoWidth,
      videoHeight: options.displayVideoHeight,
      maxScale: options.displayMaxScale,
    });
    if (videoRect.width <= 0 || videoRect.height <= 0) return null;
    return clampNormalizedPoint({
      x: (event.clientX - rect.left - videoRect.x) / videoRect.width,
      y: (event.clientY - rect.top - videoRect.y) / videoRect.height,
    });
  };

  const activateTextShape = (shape: ReviewAnnotationShape) => {
    setActiveTextEditor({ scopeKey: options.scopeKey, shapeId: shape.shapeId });
    setToolState('text');
    setColor(shape.color);
    setFontSize(shapeFontSize(shape));
  };

  const updateDraftTextShape = (shapeId: string, patch: Partial<ReviewAnnotationShape>) => {
    setDraftShapes((current) => current.map((shape) =>
      shape.shapeId === shapeId
        ? constrainAnnotationShapeWithinCanvas(
            { ...shape, ...patch },
            options.displayVideoWidth,
            options.displayVideoHeight,
          )
        : shape,
    ));
  };

  const renderedVideoDimensions = () => {
    const rect = options.stageRef.current?.getBoundingClientRect();
    if (!rect) return { width: options.displayVideoWidth, height: options.displayVideoHeight };
    const videoRect = computeContainedVideoRect({
      containerWidth: rect.width,
      containerHeight: rect.height,
      videoWidth: options.displayVideoWidth,
      videoHeight: options.displayVideoHeight,
      maxScale: options.displayMaxScale,
    });
    return {
      width: Math.max(1, videoRect.width),
      height: Math.max(1, videoRect.height),
    };
  };

  const beginDraw = (event: PointerEvent<HTMLDivElement>) => {
    if (options.readonly) return;
    const point = pointFromEvent(event);
    if (!point) return;
    const renderedVideo = renderedVideoDimensions();
    const existingShape = annotationShapeAtPoint(
      draftShapes,
      point,
      options.displayVideoWidth,
      options.displayVideoHeight,
      renderedVideo.width,
      renderedVideo.height,
    );
    if (existingShape) {
      setSelectedShapeId(existingShape.shapeId);
      closeTextEditor();
      draggingRef.current = {
        moved: false,
        origin: point,
        pointerId: event.pointerId,
        shape: existingShape,
        toolAtStart: tool,
      };
      options.videoRef.current?.pause();
      options.onPause();
      event.currentTarget.setPointerCapture?.(event.pointerId);
      return;
    }
    if (tool === 'select') return;
    setSelectedShapeId(null);
    options.videoRef.current?.pause();
    options.onPause();
    const shapeId = `draft_${createUuid()}`;
    if (tool === 'text') {
      const createdShape = makeShape(point, point, [point], shapeId);
      const shape = createdShape
        ? translateAnnotationShapeWithinCanvas(
            createdShape,
            { x: 0, y: 0 },
            options.displayVideoWidth,
            options.displayVideoHeight,
          )
        : null;
      if (shape) {
        setDraftShapes((current) => [...current, shape]);
        setRedoShapes([]);
        setSelectedShapeId(shape.shapeId);
        setActiveTextEditor({ scopeKey: options.scopeKey, shapeId: shape.shapeId });
      }
      return;
    }
    closeTextEditor();
    drawingRef.current = { start: point, points: [point], shapeId };
    const shape = makeShape(point, point, [point], shapeId);
    activeShapeRef.current = shape;
    setActiveShape(shape);
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const moveDraw = (event: PointerEvent<HTMLDivElement>) => {
    const dragging = draggingRef.current;
    if (dragging) {
      const point = pointFromEvent(event, true);
      if (!point) return;
      const renderedVideo = renderedVideoDimensions();
      const distancePx = Math.hypot(
        (point.x - dragging.origin.x) * renderedVideo.width,
        (point.y - dragging.origin.y) * renderedVideo.height,
      );
      if (!dragging.moved && distancePx < 4) return;
      dragging.moved = true;
      closeTextEditor();
      const movedShape = translateAnnotationShapeWithinCanvas(
        dragging.shape,
        {
          x: point.x - dragging.origin.x,
          y: point.y - dragging.origin.y,
        },
        options.displayVideoWidth,
        options.displayVideoHeight,
      );
      setDraftShapes((current) =>
        current.map((shape) => (shape.shapeId === movedShape.shapeId ? movedShape : shape)),
      );
      setRedoShapes([]);
      return;
    }
    const drawing = drawingRef.current;
    if (!drawing) return;
    const point = pointFromEvent(event);
    if (!point) return;
    drawing.points = [...drawing.points, point];
    const shape = makeShape(drawing.start, point, drawing.points, drawing.shapeId);
    activeShapeRef.current = shape;
    setActiveShape(shape);
  };

  const endDraw = (event?: PointerEvent<HTMLDivElement>) => {
    const dragging = draggingRef.current;
    if (dragging) {
      draggingRef.current = null;
      if (!dragging.moved && dragging.shape.tool === 'text' && dragging.toolAtStart === 'text') {
        activateTextShape(dragging.shape);
      }
      if (
        event &&
        event.currentTarget.hasPointerCapture?.(dragging.pointerId)
      ) {
        event.currentTarget.releasePointerCapture(dragging.pointerId);
      }
      return;
    }
    const shape = activeShapeRef.current;
    if (!drawingRef.current || !shape) return;
    const constrainedShape = constrainAnnotationShapeWithinCanvas(
      shape,
      options.displayVideoWidth,
      options.displayVideoHeight,
    );
    setDraftShapes((current) => [...current, constrainedShape]);
    setRedoShapes([]);
    setActiveShape(null);
    activeShapeRef.current = null;
    drawingRef.current = null;
    setSelectedShapeId(constrainedShape.shapeId);
  };

  const undo = () => {
    if (options.readonly) return;
    const removed = draftShapes[draftShapes.length - 1];
    if (!removed) return;
    setDraftShapes(draftShapes.slice(0, -1));
    setRedoShapes([removed, ...redoShapes]);
    if (removed.shapeId === activeTextShapeId) closeTextEditor();
    if (removed.shapeId === selectedShapeId) setSelectedShapeId(null);
  };

  const redo = () => {
    if (options.readonly) return;
    const [first, ...rest] = redoShapes;
    if (!first) return;
    setDraftShapes([...draftShapes, first]);
    setRedoShapes(rest);
    setSelectedShapeId(first.shapeId);
  };

  const handleColorChange = (nextColor: string) => {
    setColor(nextColor);
    if (activeTextShapeId) updateDraftTextShape(activeTextShapeId, { color: nextColor });
  };

  const handleFontSizeChange = (nextFontSize: number) => {
    const clamped = clampTextFontSize(nextFontSize);
    setFontSize(clamped);
    if (activeTextShapeId) updateDraftTextShape(activeTextShapeId, { fontSize: clamped });
  };

  return {
    activeShape,
    activeTextShapeId,
    beginDraw,
    clearDraft,
    closeTextEditor,
    color,
    draftShapes,
    endDraw,
    fontSize,
    handleColorChange,
    handleFontSizeChange,
    lineWidth,
    moveDraw,
    redo,
    redoShapes,
    selectedShapeId,
    setLineWidth,
    setTool,
    textInputRef,
    tool,
    undo,
    updateDraftTextShape,
  };
}
