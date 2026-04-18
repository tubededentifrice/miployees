import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type {
  ChatChannelBinding,
  ChatChannelKind,
  ChatGatewayProvider,
} from "@/types/api";

function fmt(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-GB", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function channelLabel(kind: ChatChannelKind): string {
  switch (kind) {
    case "offapp_whatsapp": return "WhatsApp";
    case "offapp_telegram": return "Telegram";
  }
}

function stateTone(state: ChatChannelBinding["state"]): "moss" | "sand" | "ghost" {
  if (state === "active") return "moss";
  if (state === "pending") return "sand";
  return "ghost";
}

function providerTone(
  status: ChatGatewayProvider["status"],
): "moss" | "sand" | "rust" | "ghost" {
  if (status === "connected") return "moss";
  if (status === "pending") return "sand";
  if (status === "error") return "rust";
  return "ghost";
}

export default function ChatChannelsPage() {
  const qc = useQueryClient();
  const bindings = useQuery({
    queryKey: qk.chatChannels(),
    queryFn: () => fetchJson<ChatChannelBinding[]>("/api/v1/chat/channels"),
  });
  const providers = useQuery({
    queryKey: qk.chatChannelProviders(),
    queryFn: () =>
      fetchJson<ChatGatewayProvider[]>("/api/v1/chat/channels/providers"),
  });

  const unlink = useMutation({
    mutationFn: (id: string) =>
      fetchJson<{ ok: true }>(`/api/v1/chat/channels/${id}/unlink`, {
        method: "POST",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.chatChannels() }),
  });

  const [linkAddress, setLinkAddress] = useState("");
  const startLink = useMutation({
    mutationFn: () =>
      fetchJson<{ binding_id: string }>("/api/v1/chat/channels/link/start", {
        method: "POST",
        body: {
          channel_kind: "offapp_whatsapp" as const,
          address: linkAddress,
          user_id: "u-maria",
        },
      }),
    onSuccess: () => {
      setLinkAddress("");
      qc.invalidateQueries({ queryKey: qk.chatChannels() });
    },
  });

  const sub =
    "One runtime per user, many channels — web, WhatsApp, and (deferred) Telegram. " +
    "Users link a number from their profile; every turn rides the same delegated token.";

  if (bindings.isPending || providers.isPending) {
    return (
      <DeskPage title="Chat channels" sub={sub}>
        <Loading />
      </DeskPage>
    );
  }
  if (!bindings.data || !providers.data) {
    return (
      <DeskPage title="Chat channels" sub={sub}>
        Failed to load.
      </DeskPage>
    );
  }

  return (
    <DeskPage title="Chat channels" sub={sub}>
      <section className="panel chat-channels-panel">
        <h3 className="chat-channels-panel__title">Providers</h3>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Channel</th>
              <th>Provider</th>
              <th>Status</th>
              <th>Number / handle</th>
              <th>Last webhook</th>
              <th>Templates</th>
            </tr>
          </thead>
          <tbody>
            {providers.data.map((p) => (
              <tr key={p.channel_kind}>
                <td>{channelLabel(p.channel_kind)}</td>
                <td className="mono">{p.provider}</td>
                <td>
                  <Chip tone={providerTone(p.status)} size="sm">
                    {p.status.replace("_", " ")}
                  </Chip>
                </td>
                <td className="mono">{p.display_stub}</td>
                <td className="mono">{fmt(p.last_webhook_at)}</td>
                <td>
                  {p.templates.length === 0
                    ? <span className="muted">—</span>
                    : p.templates.map((t) => (
                        <Chip key={t} tone="ghost" size="sm">{t}</Chip>
                      ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel chat-channels-panel">
        <h3 className="chat-channels-panel__title">Bindings</h3>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>User</th>
              <th>Channel</th>
              <th>Address</th>
              <th>Label</th>
              <th>State</th>
              <th>Verified</th>
              <th>Last message</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {bindings.data.map((b) => (
              <tr key={b.id}>
                <td>{b.user_display_name}</td>
                <td>{channelLabel(b.channel_kind)}</td>
                <td className="mono">{b.address}</td>
                <td>{b.display_label}</td>
                <td>
                  <Chip tone={stateTone(b.state)} size="sm">{b.state}</Chip>
                </td>
                <td className="mono">{fmt(b.verified_at)}</td>
                <td className="mono">{fmt(b.last_message_at)}</td>
                <td>
                  {b.state !== "revoked" && (
                    <button
                      className="btn btn--sm btn--ghost"
                      onClick={() => unlink.mutate(b.id)}
                      type="button"
                    >
                      Unlink
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel chat-channels-panel">
        <h3 className="chat-channels-panel__title">Link a WhatsApp number</h3>
        <p className="chat-channels-panel__hint">
          Profile-initiated challenge. A 6-digit code is sent as the{" "}
          <code>chat_channel_link_code</code> template; user replies with the
          code to activate. Mock accepts code <code>424242</code>.
        </p>
        <form
          className="chat-channels-link-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (linkAddress.trim()) startLink.mutate();
          }}
        >
          <label className="chat-channels-link-form__field">
            <span>E.164 phone number</span>
            <input
              type="tel"
              placeholder="+33 6 00 00 00 00"
              value={linkAddress}
              onChange={(e) => setLinkAddress(e.target.value)}
            />
          </label>
          <button className="btn btn--moss" type="submit">Send code</button>
        </form>
      </section>
    </DeskPage>
  );
}
