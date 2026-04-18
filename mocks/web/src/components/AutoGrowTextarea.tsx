import {
  forwardRef,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  type TextareaHTMLAttributes,
} from "react";

// Drop-in replacement for `<textarea>` that resizes to fit its
// content. By default the textarea grows without bound — no scrollbars
// ever appear. Pass `maxHeight` only for surfaces (e.g. chat composers)
// that must stay inside a fixed slot; at that point overflow becomes a
// scrollbar. Works with both controlled and uncontrolled use.
export interface AutoGrowTextareaProps
  extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  /** Optional pixel cap. Omit to let the textarea grow indefinitely. */
  maxHeight?: number;
}

const AutoGrowTextarea = forwardRef<HTMLTextAreaElement, AutoGrowTextareaProps>(
  function AutoGrowTextarea(
    { maxHeight, rows = 1, onChange, onInput, value, ...rest },
    forwarded,
  ) {
    const localRef = useRef<HTMLTextAreaElement | null>(null);
    useImperativeHandle(forwarded, () => localRef.current as HTMLTextAreaElement);

    const resize = () => {
      const el = localRef.current;
      if (!el) return;
      el.style.height = "auto";
      // box-sizing: border-box is global, so the `height` style must
      // include borders; scrollHeight does not. Without this offset
      // the element is short by `borderY` and overflows by that much.
      const cs = getComputedStyle(el);
      const borderY =
        (parseFloat(cs.borderTopWidth) || 0) +
        (parseFloat(cs.borderBottomWidth) || 0);
      const full = el.scrollHeight + borderY;
      const next = maxHeight != null ? Math.min(full, maxHeight) : full;
      el.style.height = next + "px";
      // Uncapped: suppress scrollbars unconditionally — the height
      // already matches the content. Capped: let overflow-y fall back
      // to the stylesheet so scrollbars appear once the cap is hit.
      el.style.overflowY = maxHeight != null ? "" : "hidden";
    };

    // Controlled: value prop drives size.
    useLayoutEffect(resize, [value, maxHeight]);

    return (
      <textarea
        ref={localRef}
        rows={rows}
        value={value}
        onChange={(e) => {
          onChange?.(e);
          // Uncontrolled callers don't re-render on input; resize here.
          resize();
        }}
        onInput={(e) => {
          onInput?.(e);
          resize();
        }}
        {...rest}
      />
    );
  },
);

export default AutoGrowTextarea;
