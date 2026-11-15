// A handle that is dragged or moved with the arrow keys (section 10: "Handles with role=separator announce their
// value and respond to the arrow keys"). It does not know what the value is: the caller turns it into an edit.
import { useRef, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import { valueFromDrag, valueFromKey } from "../lib/drag";

interface Props {
  label: string;
  value: number;
  min: number;
  max: number;
  valueText: string;
  className: string;
  style?: CSSProperties;
  /** Pixels for one unit of the value, read when a drag starts (the lane may have been zoomed). */
  scale: () => number;
  /** -1 for a handle that makes the value grow when it moves left. */
  sign?: 1 | -1;
  disabled?: boolean;
  onChange: (value: number) => void;
  /** The gesture is over (the pointer is up, the key is released, the focus left). */
  onCommit: () => void;
}

export function Handle({ label, value, min, max, valueText, className, style, scale, sign = 1, disabled = false, onChange, onCommit }: Props) {
  const drag = useRef<{ x: number; value: number; scale: number } | null>(null);
  const bounds = { min, max };

  const down = (event: PointerEvent<HTMLDivElement>) => {
    if (disabled) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.focus();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    drag.current = { x: event.clientX, value, scale: scale() };
  };
  const move = (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current;
    if (!start) return;
    onChange(valueFromDrag({ start: start.value, deltaPx: event.clientX - start.x, pxPerUnit: start.scale, sign, bounds }));
  };
  const up = () => {
    if (!drag.current) return;
    drag.current = null;
    onCommit();
  };
  const key = (event: KeyboardEvent<HTMLDivElement>) => {
    if (disabled || event.ctrlKey || event.metaKey || event.altKey) return;
    const next = valueFromKey(event.key, event.shiftKey, value, bounds);
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    onChange(next);
  };

  return (
    <div role="separator" aria-orientation="vertical" aria-label={label} aria-valuemin={min} aria-valuemax={max} aria-valuenow={value}
      aria-valuetext={valueText} aria-disabled={disabled || undefined} tabIndex={disabled ? -1 : 0} className={className} style={style}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}
      onKeyDown={key} onKeyUp={(event) => { if (event.key.startsWith("Arrow") || event.key === "Home" || event.key === "End") onCommit(); }} onBlur={onCommit} />
  );
}
