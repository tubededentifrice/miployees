import { Outlet } from "react-router-dom";

// The public surfaces (login, recover, enroll, guest) style their own
// top-level wrapper via `.surface--login` / `.surface--guest`, so
// PublicLayout just passes through.
export default function PublicLayout() {
  return <Outlet />;
}
