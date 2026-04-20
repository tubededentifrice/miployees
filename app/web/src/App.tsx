import { Navigate, Route, Routes } from "react-router-dom";
import PreviewShell from "@/layouts/PreviewShell";
import EmployeeLayout from "@/layouts/EmployeeLayout";
import ManagerLayout from "@/layouts/ManagerLayout";
import AdminLayout from "@/layouts/AdminLayout";
import PublicLayout from "@/layouts/PublicLayout";
import { useRole } from "@/context/RoleContext";

import TodayPage from "@/pages/employee/TodayPage";
import SchedulePage from "@/pages/employee/SchedulePage";
import TaskDetailPage from "@/pages/employee/TaskDetailPage";
import ChatPage from "@/pages/employee/ChatPage";
import MyExpensesPage from "@/pages/employee/MyExpensesPage";
import MePage from "@/pages/employee/MePage";
import HistoryPage from "@/pages/employee/HistoryPage";
import IssueNewPage from "@/pages/employee/IssueNewPage";
import EmployeeAssetPage from "@/pages/employee/EmployeeAssetPage";
import AssetScanPage from "@/pages/employee/AssetScanPage";

import DashboardPage from "@/pages/manager/DashboardPage";
import PropertiesPage from "@/pages/manager/PropertiesPage";
import PropertyDetailPage from "@/pages/manager/PropertyDetailPage";
import PropertyClosuresPage from "@/pages/manager/PropertyClosuresPage";
import EmployeesPage from "@/pages/manager/EmployeesPage";
import EmployeeDetailPage from "@/pages/manager/EmployeeDetailPage";
import EmployeeLeavesPage from "@/pages/manager/EmployeeLeavesPage";
import LeavesInboxPage from "@/pages/manager/LeavesInboxPage";
import StaysPage from "@/pages/manager/StaysPage";
import ApprovalsPage from "@/pages/manager/ApprovalsPage";
import ExpensesApprovalsPage from "@/pages/manager/ExpensesApprovalsPage";
import TemplatesPage from "@/pages/manager/TemplatesPage";
import SchedulesPage from "@/pages/manager/SchedulesPage";
import InstructionsPage from "@/pages/manager/InstructionsPage";
import InstructionDetailPage from "@/pages/manager/InstructionDetailPage";
import InventoryPage from "@/pages/manager/InventoryPage";
import AssetsPage from "@/pages/manager/AssetsPage";
import AssetDetailPage from "@/pages/manager/AssetDetailPage";
import AssetTypesPage from "@/pages/manager/AssetTypesPage";
import DocumentsPage from "@/pages/manager/DocumentsPage";
import PayPage from "@/pages/manager/PayPage";
import AuditPage from "@/pages/manager/AuditPage";
import OrganizationsPage from "@/pages/manager/OrganizationsPage";
import PermissionsPage from "@/pages/manager/PermissionsPage";
import WebhooksPage from "@/pages/manager/WebhooksPage";
import ApiTokensPage from "@/pages/manager/ApiTokensPage";
import SettingsPage from "@/pages/manager/SettingsPage";

import AdminDashboardPage from "@/pages/admin/DashboardPage";
import AdminChatGatewayPage from "@/pages/admin/ChatGatewayPage";
import AdminLlmPage from "@/pages/admin/LlmPage";
import AdminAgentDocsPage from "@/pages/admin/AgentDocsPage";
import AdminUsagePage from "@/pages/admin/UsagePage";
import AdminWorkspacesPage from "@/pages/admin/WorkspacesPage";
import AdminSettingsPage from "@/pages/admin/SettingsPage";
import AdminAdminsPage from "@/pages/admin/AdminsPage";
import AdminAuditPage from "@/pages/admin/AuditPage";

import LoginPage from "@/pages/public/LoginPage";
import RecoverPage from "@/pages/public/RecoverPage";
import AcceptPage from "@/pages/public/AcceptPage";
import GuestPage from "@/pages/public/GuestPage";

import StyleguidePage from "@/pages/StyleguidePage";
import SchedulerPage from "@/pages/SchedulerPage";

import ClientLayout from "@/layouts/ClientLayout";
import ClientPortfolioPage from "@/pages/client/PortfolioPage";
import ClientBillableHoursPage from "@/pages/client/BillableHoursPage";
import ClientQuotesPage from "@/pages/client/QuotesPage";
import ClientInvoicesPage from "@/pages/client/InvoicesPage";

function RoleHome() {
  const { role } = useRole();
  const target =
    role === "employee" ? "/today"
    : role === "client" ? "/portfolio"
    : "/dashboard";
  return <Navigate to={target} replace />;
}

// §14 — Shared routes (/today, /schedule, /my/expenses, etc.) render
// under the viewer's role-appropriate shell. Manager / Employee /
// Client each get their own layout; only `/me` is currently shared by
// all three (every persona has a profile screen).
function Shell() {
  const { role } = useRole();
  if (role === "manager") return <ManagerLayout />;
  if (role === "client") return <ClientLayout />;
  return <EmployeeLayout />;
}

export default function App() {
  const { role } = useRole();

  return (
    <Routes>
      <Route element={<PreviewShell />}>
        <Route path="/" element={<RoleHome />} />
        <Route path="/styleguide" element={<StyleguidePage />} />

        {/* Shared routes — any role. Shell picks the right layout. */}
        <Route element={<Shell />}>
          <Route path="/today" element={<TodayPage />} />
          <Route path="/schedule" element={<SchedulePage />} />
          {/* Legacy URLs — spec §14 collapses Week and /me/schedule
              into /schedule. Keep redirects so deep-links, CLI output,
              and agent tool refs continue to land on the right page. */}
          <Route path="/week" element={<Navigate to="/schedule" replace />} />
          <Route path="/me/schedule" element={<Navigate to="/schedule" replace />} />
          <Route path="/task/:tid" element={<TaskDetailPage />} />
          <Route path="/my/expenses" element={<MyExpensesPage />} />
          <Route path="/me" element={<MePage />} />
          <Route path="/scheduler" element={<SchedulerPage />} />
          {/* Legacy /bookings and /shifts URLs — spec §14 collapses
              the standalone bookings page into the /schedule day
              drawer (§09 bookings render alongside rota / tasks /
              leaves). Redirect for bookmarks and agent tool refs. */}
          <Route path="/bookings" element={<Navigate to="/schedule" replace />} />
          <Route path="/shifts" element={<Navigate to="/schedule" replace />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/issues/new" element={<IssueNewPage />} />
        </Route>

        {/* Worker-only surfaces. /chat is the worker mobile full-screen
            chat entry; on desktop both shells use AgentSidebar instead. */}
        <Route element={<EmployeeLayout />}>
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/asset/scan" element={<AssetScanPage />} />
        </Route>

        <Route element={<ManagerLayout />}>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/properties" element={<PropertiesPage />} />
          <Route path="/property/:pid" element={<PropertyDetailPage />} />
          <Route path="/property/:pid/closures" element={<PropertyClosuresPage />} />
          <Route path="/employees" element={<EmployeesPage />} />
          <Route path="/employee/:eid" element={<EmployeeDetailPage />} />
          <Route path="/employee/:eid/leaves" element={<EmployeeLeavesPage />} />
          <Route path="/leaves" element={<LeavesInboxPage />} />
          <Route path="/stays" element={<StaysPage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route
            path="/expenses"
            element={
              role === "manager" ? <ExpensesApprovalsPage /> : <Navigate to="/my/expenses" replace />
            }
          />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="/schedules" element={<SchedulesPage />} />
          <Route path="/instructions" element={<InstructionsPage />} />
          <Route path="/instructions/:iid" element={<InstructionDetailPage />} />
          <Route path="/inventory" element={<InventoryPage />} />
          <Route path="/assets" element={<AssetsPage />} />
          <Route
            path="/asset/:aid"
            element={role === "manager" ? <AssetDetailPage /> : <EmployeeAssetPage />}
          />
          <Route path="/asset_types" element={<AssetTypesPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/pay" element={<PayPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/permissions" element={<PermissionsPage />} />
          <Route path="/organizations" element={<OrganizationsPage />} />
          <Route path="/webhooks" element={<WebhooksPage />} />
          <Route path="/tokens" element={<ApiTokensPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>

        {/* /admin — bare-host deployment admin shell (§14 "Admin shell"). */}
        <Route element={<AdminLayout />}>
          <Route path="/admin" element={<Navigate to="/admin/dashboard" replace />} />
          <Route path="/admin/dashboard" element={<AdminDashboardPage />} />
          <Route path="/admin/chat-gateway" element={<AdminChatGatewayPage />} />
          <Route path="/admin/llm" element={<AdminLlmPage />} />
          <Route path="/admin/agent-docs" element={<AdminAgentDocsPage />} />
          <Route path="/admin/usage" element={<AdminUsagePage />} />
          <Route path="/admin/workspaces" element={<AdminWorkspacesPage />} />
          <Route path="/admin/signup" element={<Navigate to="/admin/settings" replace />} />
          <Route path="/admin/settings" element={<AdminSettingsPage />} />
          <Route path="/admin/admins" element={<AdminAdminsPage />} />
          <Route path="/admin/audit" element={<AdminAuditPage />} />
        </Route>

        <Route element={<ClientLayout />}>
          <Route path="/portfolio" element={<ClientPortfolioPage />} />
          <Route path="/billable_hours" element={<ClientBillableHoursPage />} />
          <Route path="/quotes" element={<ClientQuotesPage />} />
          <Route path="/invoices" element={<ClientInvoicesPage />} />
        </Route>

        <Route element={<PublicLayout />}>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/recover" element={<RecoverPage />} />
          <Route path="/accept/:token" element={<AcceptPage />} />
          <Route path="/guest/:token" element={<GuestPage />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
