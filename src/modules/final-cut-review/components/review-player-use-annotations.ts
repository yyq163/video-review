import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent,
  type RefObject,
} from 'react';
import type { NormalizedPoint, ReviewAnnotationShape } from '../contracts/types';
import { boundsFromPoints, pointerToNormalizedVideoPoint } from '../core/coordinates';
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
  const [activeShape, setActiveShape] = useState<ReviewAnnotationShape | null>(null);
  const activeShapeRef = useRef<ReviewAnnotationShape | null>(null);
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
    closeTextEditor();
    activeShapeRef.current = null;
    drawingRef.current = null;
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

  const pointFromEvent = (event: PointerEvent<HTMLDivElement>): NormalizedPoint | null => {
    const rect = options.stageRef.current?.getBoundingClientRect();
    if (!rect) return null;
    return pointerToNormalizedVideoPoint({
      clientX: event.clientX,
      clientY: event.clientY,
      containerRect: rect as RectLike,
      videoWidth: options.displayVideoWidth,
      videoHeight: options.displayVideoHeight,
      maxScale: options.displayMaxScale,
    });
  };

  const activateTextShape = (shape: ReviewAnnotationShape) => {
    setActiveTextEditor({ scopeKey: options.scopeKey, shapeId: shape.shapeId });
    setToolState('text');
    setColor(shape.color);
    setFontSize(shapeFontSize(shape));
  };

  const findDraftTextShapeAtPoint = (point: NormalizedPoint): ReviewAnnotationShape | null => {
    let nearest: { shape: ReviewAnnotationShape; distance: number } | null = null;
    for (const shape of draftShapes) {
      if (shape.tool !== 'text' || !shape.points?.[0]) continue;
      const textPoint = shape.points[0];
      const dx = Math.abs((point.x - textPoint.x) * options.displayVideoWidth);
      const dy = Math.abs((point.y - textPoint.y) * options.displayVideoHeight);
      const font = shapeFontSize(shape);
      const textLength = Math.max(2, (shape.text ?? '文字批注').length);
      const hitWidth = Math.max(56, font * textLength * 0.72);
      const hitHeight = Math.max(32, font * 1.8);
      if (dx <= hitWidth && dy <= hitHeight) {
        const distance = Math.hypot(dx, dy);
        if (!nearest || distance < nearest.distance) nearest = { shape, distance };
      }
    }
    return nearest?.shape ?? null;
  };

  const updateDraftTextShape = (shapeId: string, patch: Partial<ReviewAnnotationShape>) => {
    setDraftShapes((current) => current.map((shape) => (shape.shapeId === shapeId ? { ...shape, ...patch } : shape)));
  };

  const beginDraw = (event: PointerEvent<HTMLDivElement>) => {
    if (options.readonly) return;
    const point = pointFromEvent(event);
    if (!point) return;
    if (tool === 'select' || tool === 'text') {
      const existingTextShape = findDraftTextShapeAtPoint(point);
      if (existingTextShape) {
        activateTextShape(existingTextShape);
        return;
      }
    }
    if (tool === 'select') return;
    options.videoRef.current?.pause();
    options.onPause();
    const shapeId = `draft_${createUuid()}`;
    if (tool === 'text') {
      const shape = makeShape(point, point, [point], shapeId);
      if (shape) {
        setDraftShapes((current) => [...current, shape]);
        setRedoShapes([]);
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
    const drawing = drawingRef.current;
    if (!drawing) return;
    const point = pointFromEvent(event);
    if (!point) return;
    drawing.points = [...drawing.points, point];
    const shape = makeShape(drawing.start, point, drawing.points, drawing.shapeId);
    activeShapeRef.current = shape;
    setActiveShape(shape);
  };

  const endDraw = () => {
    const shape = activeShapeRef.current;
    if (!drawingRef.current || !shape) return;
    setDraftShapes((current) => [...current, shape]);
    setRedoShapes([]);
    setActiveShape(null);
    activeShapeRef.current = null;
    drawingRef.current = null;
  };

  const undo = () => {
    if (options.readonly) return;
    const removed = draftShapes[draftShapes.length - 1];
    if (!removed) return;
    setDraftShapes(draftShapes.slice(0, -1));
    setRedoShapes([removed, ...redoShapes]);
    if (removed.shapeId === activeTextShapeId) closeTextEditor();
  };

  const redo = () => {
    if (options.readonly) return;
    const [first, ...rest] = redoShapes;
    if (!first) return;
    setDraftShapes([...draftShapes, first]);
    setRedoShapes(rest);
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
    setLineWidth,
    setTool,
    textInputRef,
    tool,
    undo,
    updateDraftTextShape,
  };
}
