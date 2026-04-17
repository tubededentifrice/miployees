import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { AgentMessage } from "@/types/api";
import ChatLog from "@/components/chat/ChatLog";
import ChatComposer from "@/components/chat/ChatComposer";

export default function ChatPage() {
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");

  const q = useQuery({
    queryKey: qk.agentEmployeeLog(),
    queryFn: () => fetchJson<AgentMessage[]>("/api/v1/agent/employee/log"),
  });

  const send = useMutation({
    mutationFn: (body: string) =>
      fetchJson<AgentMessage>("/api/v1/agent/employee/message", {
        method: "POST", body: { body },
      }),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: qk.agentEmployeeLog() });
      const prev = qc.getQueryData<AgentMessage[]>(qk.agentEmployeeLog()) ?? [];
      const optimistic: AgentMessage = { at: new Date().toISOString(), kind: "user", body };
      qc.setQueryData<AgentMessage[]>(qk.agentEmployeeLog(), [...prev, optimistic]);
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.agentEmployeeLog(), ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: qk.agentEmployeeLog() }),
  });

  const decide = useMutation({
    mutationFn: ({ idx, decision }: { idx: number; decision: "approve" | "details" }) =>
      fetchJson<AgentMessage[]>("/api/v1/chat/action/" + idx + "/" + decision, { method: "POST" }),
    onSuccess: (log) => qc.setQueryData(qk.agentEmployeeLog(), log),
  });

  const handleSubmit = (trimmed: string) => {
    send.mutate(trimmed);
    setDraft("");
  };

  return (
    <>
      <section className="chat-screen">
        <ChatLog
          messages={q.data}
          onDecideAction={(idx, decision) => decide.mutate({ idx, decision })}
          variant="screen"
        />
      </section>

      <ChatComposer
        value={draft}
        onChange={setDraft}
        onSubmit={handleSubmit}
      />
    </>
  );
}
