import { useRef } from "react";
import { Mic, Paperclip, Send, Smile } from "lucide-react";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";

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

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed) return;
    onSubmit(trimmed);
  };

  return (
    <form
      className={"chat-composer" + (variant === "inline" ? " chat-composer--inline" : "")}
      onSubmit={(e) => { e.preventDefault(); submit(); }}
    >
      <button type="button" className="chat-composer__icon" aria-label="Attach file">
        <Paperclip size={22} strokeWidth={1.8} aria-hidden="true" />
      </button>
      <div className="chat-composer__field">
        <AutoGrowTextarea
          ref={textRef}
          rows={1}
          maxHeight={140}
          placeholder={placeholder}
          aria-label={ariaLabel}
          value={value}
          onChange={(e) => onChange(e.target.value)}
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
          <Smile size={22} strokeWidth={1.8} aria-hidden="true" />
        </button>
      </div>
      <button
        type="submit"
        className={"chat-composer__send" + (value.trim() ? " chat-composer__send--ready" : "")}
        aria-label="Send"
      >
        <span className="chat-composer__send-icon chat-composer__send-icon--mic" aria-hidden="true">
          <Mic size={22} strokeWidth={1.8} />
        </span>
        <span className="chat-composer__send-icon chat-composer__send-icon--send" aria-hidden="true">
          <Send size={22} strokeWidth={2} />
        </span>
      </button>
    </form>
  );
}
