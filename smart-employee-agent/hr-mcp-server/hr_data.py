"""
 Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.

  This software is the property of WSO2 LLC. and its suppliers, if any.
  Dissemination of any information or reproduction of any material contained
  herein is strictly forbidden, unless permitted by WSO2 in accordance with
  the WSO2 Commercial License available at http://wso2.com/licenses.
  For specific language governing the permissions and limitations under
  this license, please see the license as well as any agreement you've
  entered into with WSO2 governing the purchase of this software and any
"""

from datetime import datetime

# ─── Employees ───────────────────────────────────────────────────────────────

employees = {
    "EMP001": {
        "id": "EMP001",
        "name": "Sarah Johnson",
        "department": "Engineering",
        "role": "Software Engineer",
        "status": "in-office",
        "leave_balance": {"annual": 18, "sick": 10},
    },
    "EMP002": {
        "id": "EMP002",
        "name": "Ahmed Khan",
        "department": "Engineering",
        "role": "Senior Developer",
        "status": "in-office",
        "leave_balance": {"annual": 15, "sick": 8},
    },
    "EMP003": {
        "id": "EMP003",
        "name": "Maria Garcia",
        "department": "Engineering",
        "role": "QA Engineer",
        "status": "out-of-office",
        "leave_balance": {"annual": 20, "sick": 10},
    },
    "EMP004": {
        "id": "EMP004",
        "name": "James Wilson",
        "department": "Engineering",
        "role": "DevOps Engineer",
        "status": "in-office",
        "leave_balance": {"annual": 12, "sick": 9},
    },
}

# ─── Leave Requests ──────────────────────────────────────────────────────────

leave_requests = {
    "LR001": {
        "request_id": "LR001",
        "employee_id": "EMP001",
        "type": "Annual Leave",
        "start_date": "2026-03-10",
        "end_date": "2026-03-14",
        "days_requested": 5,
        "status": "Pending",
        "reason": "Family vacation",
        "submitted_at": "2026-02-28T09:00:00Z",
    },
    "LR002": {
        "request_id": "LR002",
        "employee_id": "EMP002",
        "type": "Sick Leave",
        "start_date": "2026-03-05",
        "end_date": "2026-03-06",
        "days_requested": 2,
        "status": "Pending",
        "reason": "Medical appointment",
        "submitted_at": "2026-03-01T08:30:00Z",
    },
    "LR003": {
        "request_id": "LR003",
        "employee_id": "EMP003",
        "type": "Annual Leave",
        "start_date": "2026-03-17",
        "end_date": "2026-03-21",
        "days_requested": 5,
        "status": "Pending",
        "reason": "Personal travel",
        "submitted_at": "2026-03-02T10:00:00Z",
    },
}

# ─── Company Holidays ────────────────────────────────────────────────────────

company_holidays = [
    {"date": "2026-01-01", "name": "New Year's Day"},
    {"date": "2026-03-20", "name": "Eid Al Fitr (expected)"},
    {"date": "2026-05-27", "name": "Arafat Day (expected)"},
    {"date": "2026-05-28", "name": "Eid Al Adha (expected)"},
    {"date": "2026-07-18", "name": "Islamic New Year (expected)"},
    {"date": "2026-12-01", "name": "Commemoration Day"},
    {"date": "2026-12-02", "name": "UAE National Day"},
]

# ─── Audit Log ───────────────────────────────────────────────────────────────

audit_log = []


# ─── Data Access Functions ───────────────────────────────────────────────────

def get_holidays() -> list[dict]:
    return company_holidays


def find_employee_by_name(name: str) -> dict | None:
    name_lower = name.lower()
    for emp in employees.values():
        if name_lower in emp["name"].lower():
            return emp
    return None


def get_pending_leave_requests() -> list[dict]:
    results = []
    for lr in leave_requests.values():
        if lr["status"] == "Pending":
            emp = employees.get(lr["employee_id"], {})
            results.append({
                "request_id": lr["request_id"],
                "employee": emp.get("name", "Unknown"),
                "type": lr["type"],
                "start_date": lr["start_date"],
                "end_date": lr["end_date"],
                "days_requested": lr["days_requested"],
                "status": lr["status"],
                "reason": lr["reason"],
            })
    return results


def get_leave_request(request_id: str) -> dict | None:
    lr = leave_requests.get(request_id)
    if not lr:
        return None
    emp = employees.get(lr["employee_id"], {})
    return {
        "request_id": lr["request_id"],
        "employee": {
            "id": emp.get("id"),
            "name": emp.get("name"),
            "department": emp.get("department"),
            "role": emp.get("role"),
        },
        "type": lr["type"],
        "start_date": lr["start_date"],
        "end_date": lr["end_date"],
        "days_requested": lr["days_requested"],
        "leave_balance": emp.get("leave_balance", {}),
        "status": lr["status"],
        "reason": lr["reason"],
        "submitted_at": lr["submitted_at"],
    }


def approve_request(request_id: str, approved_by: str) -> dict | None:
    lr = leave_requests.get(request_id)
    if not lr:
        return None
    if lr["status"] != "Pending":
        return {"error": "invalid_status", "message": f"Leave request {request_id} is already {lr['status']}."}

    lr["status"] = "Approved"
    emp = employees.get(lr["employee_id"], {})

    audit_log.append({
        "action": "approve_leave",
        "request_id": request_id,
        "employee": emp.get("name"),
        "approved_by": approved_by,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })

    return {
        "success": True,
        "request_id": request_id,
        "new_status": "Approved",
        "employee": emp.get("name"),
        "approved_by": approved_by,
        "message": f"Leave request {request_id} for {emp.get('name')} has been approved.",
    }


def get_all_leave_requests() -> list[dict]:
    """Return all leave requests for the dashboard (all statuses)."""
    results = []
    for lr in leave_requests.values():
        emp = employees.get(lr["employee_id"], {})
        results.append({
            "request_id": lr["request_id"],
            "employee": emp.get("name", "Unknown"),
            "type": lr["type"],
            "start_date": lr["start_date"],
            "end_date": lr["end_date"],
            "days_requested": lr["days_requested"],
            "status": lr["status"],
        })
    return results


def reject_request(request_id: str, reason: str, rejected_by: str) -> dict | None:
    lr = leave_requests.get(request_id)
    if not lr:
        return None
    if lr["status"] != "Pending":
        return {"error": "invalid_status", "message": f"Leave request {request_id} is already {lr['status']}."}

    lr["status"] = "Rejected"
    lr["rejection_reason"] = reason
    emp = employees.get(lr["employee_id"], {})

    audit_log.append({
        "action": "reject_leave",
        "request_id": request_id,
        "employee": emp.get("name"),
        "rejected_by": rejected_by,
        "reason": reason,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })

    return {
        "success": True,
        "request_id": request_id,
        "new_status": "Rejected",
        "employee": emp.get("name"),
        "rejected_by": rejected_by,
        "rejection_reason": reason,
        "message": f"Leave request {request_id} for {emp.get('name')} has been rejected.",
    }
