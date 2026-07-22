import type { ReviewAnnotationShape } from '../contracts/types';

export const DEFAULT_TEXT_FONT_SIZE = 32;
export const MIN_TEXT_FONT_SIZE = 12;
export const MAX_TEXT_FONT_SIZE = 96;

export function shapeLabel(shape: ReviewAnnotationShape): string {
  return shape.text ?? shape.tool;
}

export function clampTextFontSize(fontSize: number): number {
  if (!Number.isFinite(fontSize)) return DEFAULT_TEXT_FONT_SIZE;
  return Math.min(MAX_TEXT_FONT_SIZE, Math.max(MIN_TEXT_FONT_SIZE, Math.round(fontSize)));
}

export function shapeFontSize(shape: ReviewAnnotationShape): number {
  return clampTextFontSize(shape.fontSize ?? DEFAULT_TEXT_FONT_SIZE);
}
