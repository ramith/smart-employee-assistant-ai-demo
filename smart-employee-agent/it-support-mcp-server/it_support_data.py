"""
  IT Support Appointment Management - Data Module

  In-memory data store for IT support technicians, support categories,
  available time slots, and appointment records.
"""

from datetime import datetime

# ─── Support Categories ──────────────────────────────────────────────────────

support_categories = [
    {"id": "CAT001", "name": "Hardware Issue", "description": "Laptop, monitor, keyboard, mouse, docking station problems"},
    {"id": "CAT002", "name": "Software Installation", "description": "Install or update software, drivers, or development tools"},
    {"id": "CAT003", "name": "Network & Connectivity", "description": "Wi-Fi, VPN, network drive, or internet connectivity issues"},
    {"id": "CAT004", "name": "Account & Access", "description": "Password resets, access permissions, account lockouts"},
    {"id": "CAT005", "name": "Email & Communication", "description": "Email client issues, Teams, Slack, or calendar problems"},
    {"id": "CAT006", "name": "Security Incident", "description": "Suspected malware, phishing, data breach, or security concerns"},
    {"id": "CAT007", "name": "New Equipment Request", "description": "Request new laptop, monitor, peripherals, or accessories"},
    {"id": "CAT008", "name": "General IT Consultation", "description": "General IT questions, best practices, or technology guidance"},
]

# ─── IT Support Technicians ──────────────────────────────────────────────────

technicians = {
    "TECH001": {
        "id": "TECH001",
        "name": "Alex Rivera",
        "specializations": ["Hardware Issue", "New Equipment Request"],
        "status": "available",
    },
    "TECH002": {
        "id": "TECH002",
        "name": "Priya Patel",
        "specializations": ["Software Installation", "Network & Connectivity"],
        "status": "available",
    },
    "TECH003": {
        "id": "TECH003",
        "name": "Omar Hassan",
        "specializations": ["Account & Access", "Email & Communication", "Security Incident"],
        "status": "available",
    },
    "TECH004": {
        "id": "TECH004",
        "name": "Lisa Chen",
        "specializations": ["Network & Connectivity", "Security Incident", "General IT Consultation"],
        "status": "busy",
    },
}

# ─── Available Time Slots ────────────────────────────────────────────────────

available_slots = [
    {"slot_id": "SLOT001", "date": "2026-02-25", "time": "09:00", "duration_minutes": 30, "technician_id": "TECH001", "booked": False},
    {"slot_id": "SLOT002", "date": "2026-02-25", "time": "09:30", "duration_minutes": 30, "technician_id": "TECH002", "booked": False},
    {"slot_id": "SLOT003", "date": "2026-02-25", "time": "10:00", "duration_minutes": 30, "technician_id": "TECH001", "booked": False},
    {"slot_id": "SLOT004", "date": "2026-02-25", "time": "10:30", "duration_minutes": 30, "technician_id": "TECH003", "booked": False},
    {"slot_id": "SLOT005", "date": "2026-02-25", "time": "11:00", "duration_minutes": 30, "technician_id": "TECH002", "booked": True},
    {"slot_id": "SLOT006", "date": "2026-02-25", "time": "14:00", "duration_minutes": 30, "technician_id": "TECH004", "booked": False},
    {"slot_id": "SLOT007", "date": "2026-02-26", "time": "09:00", "duration_minutes": 30, "technician_id": "TECH001", "booked": False},
    {"slot_id": "SLOT008", "date": "2026-02-26", "time": "09:30", "duration_minutes": 30, "technician_id": "TECH003", "booked": False},
    {"slot_id": "SLOT009", "date": "2026-02-26", "time": "10:00", "duration_minutes": 30, "technician_id": "TECH002", "booked": False},
    {"slot_id": "SLOT010", "date": "2026-02-26", "time": "11:00", "duration_minutes": 30, "technician_id": "TECH004", "booked": False},
    {"slot_id": "SLOT011", "date": "2026-02-26", "time": "14:00", "duration_minutes": 30, "technician_id": "TECH001", "booked": False},
    {"slot_id": "SLOT012", "date": "2026-02-26", "time": "15:00", "duration_minutes": 30, "technician_id": "TECH003", "booked": False},
    {"slot_id": "SLOT013", "date": "2026-02-27", "time": "09:00", "duration_minutes": 30, "technician_id": "TECH002", "booked": False},
    {"slot_id": "SLOT014", "date": "2026-02-27", "time": "10:00", "duration_minutes": 30, "technician_id": "TECH001", "booked": False},
    {"slot_id": "SLOT015", "date": "2026-02-27", "time": "11:00", "duration_minutes": 30, "technician_id": "TECH003", "booked": False},
    {"slot_id": "SLOT016", "date": "2026-02-27", "time": "14:00", "duration_minutes": 30, "technician_id": "TECH004", "booked": False},
]

# ─── Appointments ─────────────────────────────────────────────────────────────

_next_appointment_id = 1

appointments = {
    "APT001": {
        "appointment_id": "APT001",
        "employee_name": "Sarah Johnson",
        "category_id": "CAT001",
        "slot_id": "SLOT005",
        "technician_id": "TECH002",
        "description": "Laptop screen flickering intermittently",
        "status": "Confirmed",
        "created_at": "2026-02-23T14:00:00Z",
    },
}

# ─── Audit Log ───────────────────────────────────────────────────────────────

audit_log = []


# ─── Data Access Functions ───────────────────────────────────────────────────

def get_categories() -> list[dict]:
    """Return all IT support categories."""
    return support_categories


def get_technicians_list(category_name: str | None = None) -> list[dict]:
    """Return technicians, optionally filtered by support category."""
    results = []
    for tech in technicians.values():
        if category_name and category_name not in tech["specializations"]:
            continue
        results.append({
            "id": tech["id"],
            "name": tech["name"],
            "specializations": tech["specializations"],
            "status": tech["status"],
        })
    return results


def get_available_slots_list(
    date: str | None = None,
    technician_id: str | None = None,
) -> list[dict]:
    """Return available (unbooked) time slots, optionally filtered by date and/or technician."""
    results = []
    for slot in available_slots:
        if slot["booked"]:
            continue
        if date and slot["date"] != date:
            continue
        if technician_id and slot["technician_id"] != technician_id:
            continue
        tech = technicians.get(slot["technician_id"], {})
        results.append({
            "slot_id": slot["slot_id"],
            "date": slot["date"],
            "time": slot["time"],
            "duration_minutes": slot["duration_minutes"],
            "technician": tech.get("name", "Unknown"),
            "technician_id": slot["technician_id"],
        })
    return results


def get_appointments_for_employee(employee_name: str) -> list[dict]:
    """Return all appointments for a given employee name."""
    name_lower = employee_name.lower()
    results = []
    for apt in appointments.values():
        if name_lower in apt["employee_name"].lower():
            tech = technicians.get(apt["technician_id"], {})
            slot = next((s for s in available_slots if s["slot_id"] == apt["slot_id"]), {})
            cat = next((c for c in support_categories if c["id"] == apt["category_id"]), {})
            results.append({
                "appointment_id": apt["appointment_id"],
                "category": cat.get("name", "Unknown"),
                "description": apt["description"],
                "date": slot.get("date", "N/A"),
                "time": slot.get("time", "N/A"),
                "technician": tech.get("name", "Unknown"),
                "status": apt["status"],
            })
    return results


def get_appointment(appointment_id: str) -> dict | None:
    """Return detailed information about a specific appointment."""
    apt = appointments.get(appointment_id)
    if not apt:
        return None
    tech = technicians.get(apt["technician_id"], {})
    slot = next((s for s in available_slots if s["slot_id"] == apt["slot_id"]), {})
    cat = next((c for c in support_categories if c["id"] == apt["category_id"]), {})
    return {
        "appointment_id": apt["appointment_id"],
        "employee_name": apt["employee_name"],
        "category": cat.get("name", "Unknown"),
        "category_description": cat.get("description", ""),
        "description": apt["description"],
        "date": slot.get("date", "N/A"),
        "time": slot.get("time", "N/A"),
        "duration_minutes": slot.get("duration_minutes", 30),
        "technician": {
            "id": tech.get("id"),
            "name": tech.get("name"),
            "specializations": tech.get("specializations", []),
        },
        "status": apt["status"],
        "created_at": apt["created_at"],
    }


def book_appointment(
    employee_name: str,
    category_id: str,
    slot_id: str,
    description: str,
    booked_by: str,
) -> dict:
    """Book a new IT support appointment."""
    global _next_appointment_id

    # Validate category
    cat = next((c for c in support_categories if c["id"] == category_id), None)
    if not cat:
        return {"error": "invalid_category", "message": f"Category '{category_id}' not found."}

    # Validate and reserve slot
    slot = next((s for s in available_slots if s["slot_id"] == slot_id), None)
    if not slot:
        return {"error": "invalid_slot", "message": f"Time slot '{slot_id}' not found."}
    if slot["booked"]:
        return {"error": "slot_unavailable", "message": f"Time slot '{slot_id}' is already booked."}

    # Create appointment
    apt_id = f"APT{_next_appointment_id:03d}"
    # Avoid collisions with existing hardcoded IDs
    while apt_id in appointments:
        _next_appointment_id += 1
        apt_id = f"APT{_next_appointment_id:03d}"
    _next_appointment_id += 1

    slot["booked"] = True
    tech = technicians.get(slot["technician_id"], {})

    appointments[apt_id] = {
        "appointment_id": apt_id,
        "employee_name": employee_name,
        "category_id": category_id,
        "slot_id": slot_id,
        "technician_id": slot["technician_id"],
        "description": description,
        "status": "Confirmed",
        "created_at": datetime.utcnow().isoformat() + "Z",
    }

    audit_log.append({
        "action": "book_appointment",
        "appointment_id": apt_id,
        "employee": employee_name,
        "booked_by": booked_by,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })

    return {
        "success": True,
        "appointment_id": apt_id,
        "employee_name": employee_name,
        "category": cat["name"],
        "date": slot["date"],
        "time": slot["time"],
        "technician": tech.get("name", "Unknown"),
        "status": "Confirmed",
        "message": f"Appointment {apt_id} booked for {employee_name} on {slot['date']} at {slot['time']} with {tech.get('name', 'a technician')}.",
    }


def cancel_appointment(appointment_id: str, cancelled_by: str) -> dict | None:
    """Cancel an existing appointment and free up the slot."""
    apt = appointments.get(appointment_id)
    if not apt:
        return None
    if apt["status"] == "Cancelled":
        return {"error": "already_cancelled", "message": f"Appointment {appointment_id} is already cancelled."}

    apt["status"] = "Cancelled"

    # Free up the slot
    slot = next((s for s in available_slots if s["slot_id"] == apt["slot_id"]), None)
    if slot:
        slot["booked"] = False

    audit_log.append({
        "action": "cancel_appointment",
        "appointment_id": appointment_id,
        "employee": apt["employee_name"],
        "cancelled_by": cancelled_by,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })

    return {
        "success": True,
        "appointment_id": appointment_id,
        "new_status": "Cancelled",
        "employee_name": apt["employee_name"],
        "cancelled_by": cancelled_by,
        "message": f"Appointment {appointment_id} for {apt['employee_name']} has been cancelled.",
    }


def reschedule_appointment(
    appointment_id: str,
    new_slot_id: str,
    rescheduled_by: str,
) -> dict | None:
    """Reschedule an existing appointment to a new time slot."""
    apt = appointments.get(appointment_id)
    if not apt:
        return None
    if apt["status"] == "Cancelled":
        return {"error": "already_cancelled", "message": f"Appointment {appointment_id} is cancelled and cannot be rescheduled."}

    # Validate new slot
    new_slot = next((s for s in available_slots if s["slot_id"] == new_slot_id), None)
    if not new_slot:
        return {"error": "invalid_slot", "message": f"Time slot '{new_slot_id}' not found."}
    if new_slot["booked"]:
        return {"error": "slot_unavailable", "message": f"Time slot '{new_slot_id}' is already booked."}

    # Free old slot
    old_slot = next((s for s in available_slots if s["slot_id"] == apt["slot_id"]), None)
    if old_slot:
        old_slot["booked"] = False

    # Book new slot
    new_slot["booked"] = True
    apt["slot_id"] = new_slot_id
    apt["technician_id"] = new_slot["technician_id"]

    tech = technicians.get(new_slot["technician_id"], {})

    audit_log.append({
        "action": "reschedule_appointment",
        "appointment_id": appointment_id,
        "employee": apt["employee_name"],
        "old_slot": old_slot["slot_id"] if old_slot else "N/A",
        "new_slot": new_slot_id,
        "rescheduled_by": rescheduled_by,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })

    return {
        "success": True,
        "appointment_id": appointment_id,
        "employee_name": apt["employee_name"],
        "new_date": new_slot["date"],
        "new_time": new_slot["time"],
        "technician": tech.get("name", "Unknown"),
        "status": "Confirmed",
        "message": f"Appointment {appointment_id} rescheduled to {new_slot['date']} at {new_slot['time']} with {tech.get('name', 'a technician')}.",
    }
