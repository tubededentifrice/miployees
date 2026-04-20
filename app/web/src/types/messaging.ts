// crewday — JSON API types: chat messages, chat gateway bindings,
// and gateway providers (web + off-app channels).

export type ChatChannelKind = "offapp_whatsapp" | "offapp_telegram";

export interface AgentMessage {
  at: string;
  kind: "agent" | "user" | "action";
  body: string;
  /** §23 chat gateway — channel the turn traversed; null/undefined = web. */
  channel_kind?: ChatChannelKind | null;
}

export interface ChatChannelBinding {
  id: string;
  user_id: string;
  user_display_name: string;
  channel_kind: ChatChannelKind;
  address: string;
  display_label: string;
  state: "pending" | "active" | "revoked";
  verified_at: string | null;
  last_message_at: string | null;
  revoked_at: string | null;
  revoke_reason: "user" | "stop_keyword" | "user_archived" | "admin" | "provider_error" | null;
}

export interface ChatGatewayProvider {
  channel_kind: ChatChannelKind;
  provider: string;
  status: "connected" | "pending" | "error" | "not_configured";
  display_stub: string;
  last_webhook_at: string | null;
  templates: string[];
}
