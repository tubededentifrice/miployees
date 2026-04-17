import { useEffect, useRef } from "react";
import type { AgentMessage } from "@/types/api";

function hhmm(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

type ActionDecision = "approve" | "details";

export interface ChatLogProps {
  messages: AgentMessage[] | undefined;
  onDecideAction?: (idx: number, decision: ActionDecision) => void;
  /** Applied to the outer `.chat-log`. `chat-log--inline` removes the
   *  flex:1 scroll-box behaviour so the log flows inside a regular page. */
  variant?: "screen" | "inline";
  ariaLabel?: string;
}

export default function ChatLog({
  messages,
  onDecideAction,
  variant = "screen",
  ariaLabel,
}: ChatLogProps) {
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (variant !== "screen") return;
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages?.length, variant]);

  const className = variant === "inline" ? "chat-log chat-log--inline" : "chat-log";

  return (
    <div
      className={className}
      role="log"
      aria-live="polite"
      aria-label={ariaLabel}
      ref={logRef}
    >
      {messages?.map((m, idx) => {
        if (m.kind === "action") {
          return (
            <div key={idx} className="chat-msg chat-msg--action">
              <span className="chat-msg__body">{m.body}</span>
              {onDecideAction && (
                <div className="chat-msg__ctas">
                  <button
                    className="btn btn--moss btn--sm"
                    type="button"
                    onClick={() => onDecideAction(idx, "approve")}
                  >
                    Approve
                  </button>
                  <button
                    className="btn btn--ghost btn--sm"
                    type="button"
                    onClick={() => onDecideAction(idx, "details")}
                  >
                    Details
                  </button>
                </div>
              )}
              <span className="chat-msg__time">{hhmm(m.at)}</span>
            </div>
          );
        }
        return (
          <div key={idx} className={"chat-msg chat-msg--" + m.kind}>
            <span className="chat-msg__body">{m.body}</span>
            <span className="chat-msg__time">{hhmm(m.at)}</span>
          </div>
        );
      })}
    </div>
  );
}
