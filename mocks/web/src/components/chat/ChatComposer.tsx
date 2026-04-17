import { useRef } from "react";

export interface ChatComposerProps {
  value: string;
  onChange: (next: string) => void;
  onSubmit: (trimmed: string) => void;
  placeholder?: string;
  ariaLabel?: string;
  /** `inline` lets the composer flow inside a regular page section rather
   *  than sticking to the viewport bottom like the full-screen `/chat`. */
  variant?: "screen" | "inline";
}

export default function ChatComposer({
  value,
  onChange,
  onSubmit,
  placeholder = "Message",
  ariaLabel = "Message",
  variant = "screen",
}: ChatComposerProps) {
  const textRef = useRef<HTMLTextAreaElement>(null);

  const autogrow = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 140) + "px";
  };

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed) return;
    onSubmit(trimmed);
    if (textRef.current) textRef.current.style.height = "auto";
  };

  return (
    <form
      className={"chat-composer" + (variant === "inline" ? " chat-composer--inline" : "")}
      onSubmit={(e) => { e.preventDefault(); submit(); }}
    >
      <button type="button" className="chat-composer__icon" aria-label="Attach file">
        <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor"
             strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21.44 11.05 12.25 20.24a6 6 0 0 1-8.49-8.49l9.2-9.19a4 4 0 0 1 5.66 5.65l-9.2 9.19a2 2 0 0 1-2.83-2.82l8.49-8.49" />
        </svg>
      </button>
      <div className="chat-composer__field">
        <textarea
          ref={textRef}
          rows={1}
          placeholder={placeholder}
          aria-label={ariaLabel}
          value={value}
          onChange={(e) => { onChange(e.target.value); autogrow(e.currentTarget); }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <button
          type="button"
          className="chat-composer__icon chat-composer__icon--inline"
          aria-label="Emoji"
        >
          <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor"
               strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx={12} cy={12} r={9} />
            <path d="M8.5 14s1.5 2 3.5 2 3.5-2 3.5-2" />
            <line x1={9} y1={9.5} x2={9} y2={9.5} />
            <line x1={15} y1={9.5} x2={15} y2={9.5} />
          </svg>
        </button>
      </div>
      <button
        type="submit"
        className={"chat-composer__send" + (value.trim() ? " chat-composer__send--ready" : "")}
        aria-label="Send"
      >
        <span className="chat-composer__send-icon chat-composer__send-icon--mic" aria-hidden="true">
          <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor"
               strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
            <rect x={9} y={3} width={6} height={12} rx={3} />
            <path d="M5 11a7 7 0 0 0 14 0" />
            <line x1={12} y1={18} x2={12} y2={22} />
          </svg>
        </span>
        <span className="chat-composer__send-icon chat-composer__send-icon--send" aria-hidden="true">
          <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor"
               strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <path d="m4 12 16-8-6 18-3-8-7-2Z" />
          </svg>
        </span>
      </button>
    </form>
  );
}
