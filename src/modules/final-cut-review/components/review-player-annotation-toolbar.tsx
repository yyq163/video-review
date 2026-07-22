import type { ReactElement } from 'react';
import {
  ALargeSmall,
  Circle,
  CornerDownRight,
  MousePointer2,
  Palette,
  PenLine,
  RectangleHorizontal,
  Redo2,
  RotateCcw,
  Type,
  Undo2,
} from 'lucide-react';
import {
  MAX_TEXT_FONT_SIZE,
  MIN_TEXT_FONT_SIZE,
} from './review-player-annotation-utils';
import type { AnnotationEditorTool } from './review-player-types';

interface AnnotationToolbarProps {
  tool: AnnotationEditorTool;
  color: string;
  lineWidth: number;
  fontSize: number;
  readonlyReason?: string;
  canUndo: boolean;
  canRedo: boolean;
  onTool(tool: AnnotationEditorTool): void;
  onColor(color: string): void;
  onLineWidth(lineWidth: number): void;
  onFontSize(fontSize: number): void;
  onUndo(): void;
  onRedo(): void;
  onClear(): void;
}

const TOOLS: Array<{ tool: AnnotationEditorTool; label: string; icon: ReactElement }> = [
  { tool: 'select', label: '选择', icon: <MousePointer2 /> },
  { tool: 'rect', label: '矩形', icon: <RectangleHorizontal /> },
  { tool: 'circle', label: '圆形', icon: <Circle /> },
  { tool: 'arrow', label: '箭头', icon: <CornerDownRight /> },
  { tool: 'pen', label: '画笔', icon: <PenLine /> },
  { tool: 'text', label: '文字', icon: <Type /> },
];

const COLORS = ['#57e3d2', '#f3576a', '#ffcc3d', '#ffffff'];

export function AnnotationToolbar(props: AnnotationToolbarProps) {
  const isReadonly = Boolean(props.readonlyReason);
  return (
    <div className="fj-review-floating-toolbar" data-testid="annotation-toolbar" aria-label="标注工具栏">
      {TOOLS.map((item) => (
        <button
          key={item.tool}
          type="button"
          title={props.readonlyReason ? `${item.label}：${props.readonlyReason}` : item.label}
          aria-label={item.label}
          disabled={isReadonly}
          className={props.tool === item.tool ? 'is-active' : ''}
          onClick={() => props.onTool(item.tool)}
        >
          {item.icon}
        </button>
      ))}
      {COLORS.map((color) => (
        <button
          key={color}
          type="button"
          title={props.readonlyReason ? `颜色 ${color}：${props.readonlyReason}` : `颜色 ${color}`}
          aria-label={`颜色 ${color}`}
          disabled={isReadonly}
          className={props.color === color ? 'is-active' : ''}
          onClick={() => props.onColor(color)}
        >
          <span className="fj-review-color-dot" style={{ background: color }} />
        </button>
      ))}
      <label
        className="fj-review-custom-color"
        title={props.readonlyReason ? `自定义颜色：${props.readonlyReason}` : '自定义颜色'}
      >
        <Palette aria-hidden="true" />
        <input
          aria-label="自定义颜色"
          type="color"
          value={props.color}
          disabled={isReadonly}
          onChange={(event) => props.onColor(event.currentTarget.value)}
        />
      </label>
      {props.tool === 'text' ? (
        <label className="fj-review-font-size" title={`文字字号 ${props.fontSize}`}>
          <ALargeSmall aria-hidden="true" />
          <input
            aria-label="文字字号"
            type="number"
            min={MIN_TEXT_FONT_SIZE}
            max={MAX_TEXT_FONT_SIZE}
            step={2}
            value={props.fontSize}
            disabled={isReadonly}
            onChange={(event) => props.onFontSize(Number(event.target.value))}
          />
        </label>
      ) : (
        <label className="fj-review-line-width">
          <span>线宽</span>
          <input
            aria-label="线宽"
            type="range"
            min={1}
            max={12}
            value={props.lineWidth}
            disabled={isReadonly}
            onChange={(event) => props.onLineWidth(Number(event.target.value))}
          />
        </label>
      )}
      <button type="button" title="撤销" aria-label="撤销" disabled={isReadonly || !props.canUndo} onClick={props.onUndo}>
        <Undo2 />
      </button>
      <button type="button" title="重做" aria-label="重做" disabled={isReadonly || !props.canRedo} onClick={props.onRedo}>
        <Redo2 />
      </button>
      <button type="button" title="重置草稿" aria-label="重置草稿" disabled={isReadonly} onClick={props.onClear}>
        <RotateCcw />
      </button>
    </div>
  );
}
