"""Expenses context — expense claims, receipts, OCR.

The v1 surface (cd-7rfu) ships claim CRUD + the draft → submitted →
cancel state machine in :mod:`app.domain.expenses.claims`. Manager
approval + reimbursement (cd-9guk) layers the
``submitted -> approved -> reimbursed`` / ``submitted -> rejected``
transitions in :mod:`app.domain.expenses.approval`. OCR autofill
(cd-95zb) is a follow-up.

See ``docs/specs/09-time-payroll-expenses.md`` §"Expense claims".
"""

from app.domain.expenses.approval import (
    ApprovalEdits,
    ApprovalPermissionDenied,
    ClaimNotApprovable,
    ClaimNotReimbursable,
    RejectBody,
    ReimburseBody,
    ReimbursePermissionDenied,
    ReimburseVia,
    approve_claim,
    list_pending,
    mark_reimbursed,
    reject_claim,
)
from app.domain.expenses.claims import (
    BlobMimeNotAllowed,
    BlobMissing,
    BlobTooLarge,
    ClaimNotEditable,
    ClaimNotFound,
    ClaimPermissionDenied,
    ClaimStateTransitionInvalid,
    CurrencyInvalid,
    ExpenseAttachmentView,
    ExpenseCategory,
    ExpenseClaimCreate,
    ExpenseClaimUpdate,
    ExpenseClaimView,
    ExpenseState,
    PurchaseDateInFuture,
    ReceiptAttach,
    ReceiptKind,
    TooManyAttachments,
    attach_receipt,
    cancel_claim,
    create_claim,
    detach_receipt,
    get_claim,
    list_for_user,
    list_for_workspace,
    submit_claim,
    update_claim,
)

__all__ = [
    "ApprovalEdits",
    "ApprovalPermissionDenied",
    "BlobMimeNotAllowed",
    "BlobMissing",
    "BlobTooLarge",
    "ClaimNotApprovable",
    "ClaimNotEditable",
    "ClaimNotFound",
    "ClaimNotReimbursable",
    "ClaimPermissionDenied",
    "ClaimStateTransitionInvalid",
    "CurrencyInvalid",
    "ExpenseAttachmentView",
    "ExpenseCategory",
    "ExpenseClaimCreate",
    "ExpenseClaimUpdate",
    "ExpenseClaimView",
    "ExpenseState",
    "PurchaseDateInFuture",
    "ReceiptAttach",
    "ReceiptKind",
    "RejectBody",
    "ReimburseBody",
    "ReimbursePermissionDenied",
    "ReimburseVia",
    "TooManyAttachments",
    "approve_claim",
    "attach_receipt",
    "cancel_claim",
    "create_claim",
    "detach_receipt",
    "get_claim",
    "list_for_user",
    "list_for_workspace",
    "list_pending",
    "mark_reimbursed",
    "reject_claim",
    "submit_claim",
    "update_claim",
]
