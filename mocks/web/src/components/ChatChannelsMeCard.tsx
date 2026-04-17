import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type {
  ChatChannelBinding,
  ChatChannelKind,
  Me,
} from "@/types/api";

// §23 — per-user "Chat channels" section for /me. Lists the current
// user's bindings with state chips and Unlink, provides a two-step
// link ceremony that posts to /api/v1/chat/channels/link/{start,verify}
// (mock code 424242), and exposes preferred_offapp_channel + quiet
// hours via PUT /me/offapp_preferences. Shares the styling language
// with the manager-facing ChatChannelsPage (panel + table patterns),
// scaled down for the phone surface.

function channelLabel(kind: ChatChannelKind): string {
  switch (kind) {
    case "offapp_whatsapp": return "WhatsApp";
    case "offapp_sms": return "SMS";
    case "offapp_telegram": return "Telegram";
  }
}

function stateTone(state: ChatChannelBinding["state"]): "moss" | "sand" | "ghost" {
  if (state === "active") return "moss";
  if (state === "pending") return "sand";
  return "ghost";
}

function fmt(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-GB", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

interface OffappPrefs {
  preferred_offapp_channel: Me["preferred_offapp_channel"];
  quiet_hours_start: string;
  quiet_hours_end: string;
}

export default function ChatChannelsMeCard({ me }: { me: Me }) {
  const qc = useQueryClient();
  const myUserId = me.user_id;

  const bindingsQ = useQuery({
    queryKey: qk.chatChannels(),
    queryFn: () => fetchJson<ChatChannelBinding[]>("/api/v1/chat/channels"),
  });

  const myBindings = (bindingsQ.data ?? []).filter((b) => b.user_id === myUserId);

  const [pendingId, setPendingId] = useState<string | null>(
    myBindings.find((b) => b.state === "pending")?.id ?? null,
  );
  const [address, setAddress] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const startLink = useMutation({
    mutationFn: () =>
      fetchJson<{ binding_id: string; state: string; hint: string }>(
        "/api/v1/chat/channels/link/start",
        {
          method: "POST",
          body: {
            channel_kind: "offapp_whatsapp" as const,
            address,
            user_id: myUserId,
          },
        },
      ),
    onSuccess: (r) => {
      setPendingId(r.binding_id);
      setError(null);
      qc.invalidateQueries({ queryKey: qk.chatChannels() });
    },
    onError: () => setError("Could not start the link ceremony."),
  });

  const verifyLink = useMutation({
    mutationFn: () =>
      fetchJson<{ binding_id: string; state: string }>(
        "/api/v1/chat/channels/link/verify",
        { method: "POST", body: { binding_id: pendingId, code } },
      ),
    onSuccess: () => {
      setCode("");
      setAddress("");
      setPendingId(null);
      setError(null);
      qc.invalidateQueries({ queryKey: qk.chatChannels() });
    },
    onError: () => setError("Wrong code — try 424242 in the mock."),
  });

  const unlink = useMutation({
    mutationFn: (id: string) =>
      fetchJson<{ binding_id: string; state: string }>(
        `/api/v1/chat/channels/${id}/unlink`,
        { method: "POST" },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.chatChannels() });
    },
  });

  const savePrefs = useMutation({
    mutationFn: (patch: Partial<OffappPrefs>) =>
      fetchJson<OffappPrefs>("/api/v1/me/offapp_preferences", {
        method: "PUT",
        body: patch,
      }),
    onSuccess: (next) => {
      qc.setQueryData<Me | undefined>(qk.me(), (prev) =>
        prev ? { ...prev, ...next } : prev,
      );
    },
  });

  return (
    <section className="phone__section">
      <h2 className="section-title">Chat channels</h2>
      <p className="me-chat-channels__intro">
        Reach your agent over WhatsApp. Messages you send land in the
        same conversation as the Chat tab. Your agent only reaches out
        during the hours you allow below.
      </p>

      {bindingsQ.isPending && <p className="muted">Loading bindings…</p>}
      {!bindingsQ.isPending && myBindings.length === 0 && (
        <p className="muted me-chat-channels__empty">
          No channels linked yet.
        </p>
      )}

      <ul className="me-chat-channels__list">
        {myBindings.map((b) => (
          <li key={b.id} className="me-chat-channels__binding">
            <div className="me-chat-channels__binding-head">
              <strong>{channelLabel(b.channel_kind)}</strong>
              <span className={"chip chip--sm chip--" + stateTone(b.state)}>
                {b.state}
              </span>
            </div>
            <div className="me-chat-channels__binding-meta mono">
              {b.address}
            </div>
            <div className="me-chat-channels__binding-meta muted">
              {b.display_label}
              {b.state === "active" && b.last_message_at
                ? " · last used " + fmt(b.last_message_at)
                : ""}
              {b.state === "revoked" && b.revoke_reason
                ? " · revoked (" + b.revoke_reason.replace("_", " ") + ")"
                : ""}
            </div>
            {b.state !== "revoked" && (
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => unlink.mutate(b.id)}
              >
                Unlink
              </button>
            )}
          </li>
        ))}
      </ul>

      <div className="me-chat-channels__link">
        <h3 className="me-chat-channels__subtitle">Link WhatsApp</h3>
        {pendingId ? (
          <form
            className="me-chat-channels__form"
            onSubmit={(e) => {
              e.preventDefault();
              if (code.trim()) verifyLink.mutate();
            }}
          >
            <label className="me-chat-channels__field">
              <span>6-digit code</span>
              <input
                inputMode="numeric"
                pattern="\d{6}"
                maxLength={6}
                placeholder="424242"
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            </label>
            <div className="me-chat-channels__actions">
              <button className="btn btn--moss btn--sm" type="submit">
                Verify
              </button>
              <button
                className="btn btn--ghost btn--sm"
                type="button"
                onClick={() => {
                  setPendingId(null);
                  setCode("");
                }}
              >
                Cancel
              </button>
            </div>
            <p className="me-chat-channels__hint">
              A code was sent via the <code>chat_channel_link_code</code>{" "}
              template. Mock accepts <code>424242</code>.
            </p>
          </form>
        ) : (
          <form
            className="me-chat-channels__form"
            onSubmit={(e) => {
              e.preventDefault();
              if (address.trim()) startLink.mutate();
            }}
          >
            <label className="me-chat-channels__field">
              <span>E.164 phone number</span>
              <input
                type="tel"
                placeholder="+33 6 00 00 00 00"
                value={address}
                onChange={(e) => setAddress(e.target.value)}
              />
            </label>
            <button className="btn btn--moss btn--sm" type="submit">
              Send code
            </button>
          </form>
        )}
        {error && <p className="me-chat-channels__error">{error}</p>}
      </div>

      <div className="me-chat-channels__prefs">
        <h3 className="me-chat-channels__subtitle">Reach-out preference</h3>
        <div className="me-chat-channels__radio-row" role="radiogroup">
          {(["whatsapp", "sms", "none"] as const).map((opt) => (
            <label key={opt} className="me-chat-channels__radio">
              <input
                type="radio"
                name="preferred_offapp_channel"
                value={opt}
                checked={me.preferred_offapp_channel === opt}
                onChange={() =>
                  savePrefs.mutate({ preferred_offapp_channel: opt })
                }
              />
              <span>
                {opt === "whatsapp"
                  ? "WhatsApp"
                  : opt === "sms"
                  ? "SMS (agent → you only)"
                  : "No off-app reach-out"}
              </span>
            </label>
          ))}
        </div>
        <p className="me-chat-channels__hint">
          <strong>None</strong> is a hard opt-out — the agent will queue
          replies for the web app instead.
        </p>
      </div>

      <div className="me-chat-channels__prefs">
        <h3 className="me-chat-channels__subtitle">Quiet hours</h3>
        <div className="me-chat-channels__times">
          <label className="me-chat-channels__field">
            <span>From</span>
            <input
              type="time"
              value={me.quiet_hours_start}
              onChange={(e) =>
                savePrefs.mutate({ quiet_hours_start: e.target.value })
              }
            />
          </label>
          <label className="me-chat-channels__field">
            <span>To</span>
            <input
              type="time"
              value={me.quiet_hours_end}
              onChange={(e) =>
                savePrefs.mutate({ quiet_hours_end: e.target.value })
              }
            />
          </label>
        </div>
        <p className="me-chat-channels__hint">
          Your agent won't reach out over WhatsApp or SMS during this
          window. Replying yourself re-opens the conversation at any
          time.
        </p>
      </div>
    </section>
  );
}
