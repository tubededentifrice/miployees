// Parent-route resolution for the header back button. Spec §14 "Page
// header" — sub-pages get their back affordance from this map so
// every one of them lands on the same consistent left-chevron
// icon-button. A page that wants a non-default parent passes an
// explicit `back={{ to, label }}` to PageHeader.
//
// The map is ordered: first pattern that matches wins. Patterns are
// simple prefix strings; dynamic segments are implicit (everything
// after the prefix is treated as the `id`).

export interface ParentDescriptor {
  to: string;
  label: string;
}

const RULES: Array<{ prefix: string; parent: ParentDescriptor }> = [
  // Employee / worker surfaces
  { prefix: "/task/", parent: { to: "/today", label: "Today" } },
  { prefix: "/asset/scan", parent: { to: "/today", label: "Today" } },
  { prefix: "/asset/", parent: { to: "/assets", label: "Assets" } },
  { prefix: "/issues/new", parent: { to: "/me", label: "Me" } },
  { prefix: "/history", parent: { to: "/me", label: "Me" } },
  // Manager / admin detail surfaces
  { prefix: "/property/", parent: { to: "/properties", label: "Properties" } },
  { prefix: "/user/", parent: { to: "/users", label: "Users" } },
  { prefix: "/employee/", parent: { to: "/employees", label: "Employees" } },
  { prefix: "/instructions/", parent: { to: "/instructions", label: "Instructions" } },
  // Approvals inbox rows
  { prefix: "/leaves/", parent: { to: "/leaves", label: "Leaves" } },
];

export function resolveParent(pathname: string): ParentDescriptor | null {
  // RULES only lists sub-page prefixes, never top-level tabs, so
  // `startsWith` alone correctly resolves both exact-path rules
  // (e.g. `/history`) and dynamic ones (e.g. `/task/`).
  for (const { prefix, parent } of RULES) {
    if (pathname.startsWith(prefix)) {
      return parent;
    }
  }
  return null;
}
