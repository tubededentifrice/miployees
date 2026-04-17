import { Navigate, Route, Routes } from "react-router-dom";
import PreviewShell from "@/layouts/PreviewShell";
import EmployeeLayout from "@/layouts/EmployeeLayout";
import ManagerLayout from "@/layouts/ManagerLayout";
import PublicLayout from "@/layouts/PublicLayout";
import { useRole } from "@/context/RoleContext";

import TodayPage from "@/pages/employee/TodayPage";
import WeekPage from "@/pages/employee/WeekPage";
import TaskDetailPage from "@/pages/employee/TaskDetailPage";
import ChatPage from "@/pages/employee/ChatPage";
import MyExpensesPage from "@/pages/employee/MyExpensesPage";
import MePage from "@/pages/employee/MePage";
import ShiftsPage from "@/pages/employee/ShiftsPage";
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
import PermissionsPage from "@/pages/manager/PermissionsPage";
import WebhooksPage from "@/pages/manager/WebhooksPage";
import LlmPage from "@/pages/manager/LlmPage";
import SettingsPage from "@/pages/manager/SettingsPage";

import LoginPage from "@/pages/public/LoginPage";
import RecoverPage from "@/pages/public/RecoverPage";
import EnrollPage from "@/pages/public/EnrollPage";
import GuestPage from "@/pages/public/GuestPage";

import StyleguidePage from "@/pages/StyleguidePage";

function RoleHome() {
  const { role } = useRole();
  return <Navigate to={role === "employee" ? "/today" : "/dashboard"} replace />;
}

export default function App() {
  const { role } = useRole();

  return (
    <Routes>
      <Route element={<PreviewShell />}>
        <Route path="/" element={<RoleHome />} />
        <Route path="/styleguide" element={<StyleguidePage />} />

        <Route element={<EmployeeLayout />}>
          {/* If /today is hit as manager, bounce to dashboard (mirrors legacy). */}
          <Route
            path="/today"
            element={role === "manager" ? <Navigate to="/dashboard" replace /> : <TodayPage />}
          />
          <Route path="/week" element={<WeekPage />} />
          <Route path="/task/:tid" element={<TaskDetailPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/my/expenses" element={<MyExpensesPage />} />
          <Route path="/me" element={<MePage />} />
          <Route path="/shifts" element={<ShiftsPage />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/issues/new" element={<IssueNewPage />} />
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
          <Route path="/webhooks" element={<WebhooksPage />} />
          <Route path="/llm" element={<LlmPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>

        <Route element={<PublicLayout />}>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/recover" element={<RecoverPage />} />
          <Route path="/enroll/:token" element={<EnrollPage />} />
          <Route path="/guest/:token" element={<GuestPage />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
