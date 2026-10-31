"""
NetProvision API
================
POST /api/vlans/provision          — provision a VLAN across switches
GET  /api/vlans/provision/{job_id} — job status + step results
POST /api/vlans/provision/{job_id}/rollback — rollback a failed job

GET  /api/inventory/devices              — device list
GET  /api/inventory/devices/{hostname}   — device detail
GET  /api/inventory/devices/{hostname}/interfaces
GET  /api/inventory/devices/{hostname}/vlans
GET  /api/inventory/vlans               — all VLANs across all devices
GET  /api/inventory/vlans/{vlan_id}     — where a VLAN exists
GET  /api/inventory/interfaces/vlan/{vlan_id} — ports using this VLAN

POST /api/inventory/refresh/{hostname}  — re-collect device inventory

GET  /api/jobs                          — recent provisioning jobs
GET  /                                  — web dashboard
"""

from __future__ import annotations
import os
import sys
import uuid
import threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
from typing import Optional

try:
    from fastapi import FastAPI, HTTPException, BackgroundTasks
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse
    from pydantic import BaseModel, Field
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from models import (
    VLANProvisionRequest, ProvisioningJob, JobStatus,
    DeviceCredentials,
)
from inventory.store import InventoryStore
from provisioner.engine import validate, build_plan, execute, rollback


# ── Pydantic request bodies ───────────────────────────────────────────────────

if FASTAPI_AVAILABLE:
    class ProvisionRequest(BaseModel):
        vlan_id:       int        = Field(..., ge=2, le=4093)
        name:          str        = Field(..., min_length=1, max_length=32)
        switches:      list[str]  = Field(default_factory=list)
        add_to_trunks: list[str]  = Field(default_factory=list)
        access_ports:  list[str]  = Field(default_factory=list)
        description:   str        = ""
        requested_by:  str        = "api"


def build_app(store: InventoryStore, device_map: dict[str, DeviceCredentials]) -> "FastAPI":
    if not FASTAPI_AVAILABLE:
        raise RuntimeError("pip install fastapi uvicorn")

    app = FastAPI(title="NetProvision", version="1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

    # ── Provisioning ──────────────────────────────────────────────────────────

    @app.post("/api/vlans/provision", status_code=202)
    def provision_vlan(body: ProvisionRequest, background_tasks: BackgroundTasks):
        req = VLANProvisionRequest(
            vlan_id       = body.vlan_id,
            name          = body.name,
            switches      = body.switches,
            add_to_trunks = body.add_to_trunks,
            access_ports  = body.access_ports,
            description   = body.description,
            requested_by  = body.requested_by,
        )

        # Validate synchronously — fast, no SSH
        errors = validate(req, store, device_map)
        if errors:
            raise HTTPException(422, detail={"errors": errors})

        job = ProvisioningJob(
            job_id     = str(uuid.uuid4()),
            request    = req,
            status     = JobStatus.PENDING,
            steps      = build_plan(req),
            created_at = datetime.now(timezone.utc),
        )
        store.write_job(job)

        background_tasks.add_task(execute, job, store, device_map)

        return {
            "job_id":    job.job_id,
            "status":    job.status.value,
            "steps":     len(job.steps),
            "message":   f"Provisioning VLAN {req.vlan_id} ({req.name}) — {len(job.steps)} steps queued",
        }

    @app.get("/api/vlans/provision/{job_id}")
    def get_job_status(job_id: str):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, f"Job '{job_id}' not found")
        return _job_to_dict(job)

    @app.post("/api/vlans/provision/{job_id}/rollback")
    def rollback_job(job_id: str, background_tasks: BackgroundTasks):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, f"Job '{job_id}' not found")
        if job.status not in (JobStatus.FAILED, JobStatus.SUCCESS):
            raise HTTPException(409, f"Job is in state '{job.status.value}' — only FAILED or SUCCESS jobs can be rolled back")
        background_tasks.add_task(rollback, job, store, device_map)
        return {"job_id": job_id, "message": "Rollback queued"}

    # ── Inventory — devices ───────────────────────────────────────────────────

    @app.get("/api/inventory/devices")
    def list_devices():
        devices = store.list_devices()
        return [_device_summary(d, store) for d in devices]

    @app.get("/api/inventory/devices/{hostname}")
    def get_device(hostname: str):
        device = store.get_device(hostname)
        if not device:
            raise HTTPException(404, f"Device '{hostname}' not in inventory")
        ifaces = store.get_interfaces(hostname)
        vlans  = store.get_vlans(device=hostname)
        return {
            "hostname":    device.hostname,
            "ip":          device.ip,
            "device_type": device.device_type,
            "platform":    device.platform,
            "model":       device.model,
            "serial":      device.serial,
            "uptime":      device.uptime,
            "tags":        device.tags,
            "last_seen":   device.last_seen.isoformat() if device.last_seen else None,
            "interface_count": len(ifaces),
            "vlan_count":      len(vlans),
        }

    @app.get("/api/inventory/devices/{hostname}/interfaces")
    def get_device_interfaces(hostname: str):
        if not store.get_device(hostname):
            raise HTTPException(404, f"Device '{hostname}' not in inventory")
        return [_iface_to_dict(i) for i in store.get_interfaces(hostname)]

    @app.get("/api/inventory/devices/{hostname}/vlans")
    def get_device_vlans(hostname: str):
        if not store.get_device(hostname):
            raise HTTPException(404, f"Device '{hostname}' not in inventory")
        return [_vlan_to_dict(v) for v in store.get_vlans(device=hostname)]

    # ── Inventory — VLANs ─────────────────────────────────────────────────────

    @app.get("/api/inventory/vlans")
    def list_vlans(device: Optional[str] = None):
        return [_vlan_to_dict(v) for v in store.get_vlans(device=device)]

    @app.get("/api/inventory/vlans/{vlan_id}")
    def get_vlan(vlan_id: int):
        vlans = [v for v in store.get_vlans() if v.vlan_id == vlan_id]
        if not vlans:
            raise HTTPException(404, f"VLAN {vlan_id} not found in inventory")
        ifaces = store.find_interfaces_by_vlan(vlan_id)
        return {
            "vlan_id":    vlan_id,
            "name":       vlans[0].name,
            "devices":    [v.device for v in vlans],
            "interfaces": [_iface_to_dict(i) for i in ifaces],
        }

    @app.get("/api/inventory/interfaces/vlan/{vlan_id}")
    def interfaces_for_vlan(vlan_id: int):
        return [_iface_to_dict(i) for i in store.find_interfaces_by_vlan(vlan_id)]

    # ── Inventory refresh ─────────────────────────────────────────────────────

    @app.post("/api/inventory/refresh/{hostname}")
    def refresh_inventory(hostname: str, background_tasks: BackgroundTasks):
        creds = device_map.get(hostname)
        if not creds:
            raise HTTPException(404, f"Device '{hostname}' not in config")
        from inventory.collector import collect
        background_tasks.add_task(collect, creds, store)
        return {"hostname": hostname, "message": "Inventory refresh queued"}

    # ── Jobs ──────────────────────────────────────────────────────────────────

    @app.get("/api/jobs")
    def list_jobs(limit: int = 50):
        return [_job_to_dict(j) for j in store.list_jobs(limit=limit)]

    # ── Dashboard ─────────────────────────────────────────────────────────────
    web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
    if os.path.isdir(web_dir):
        @app.get("/", include_in_schema=False)
        def dashboard():
            return FileResponse(os.path.join(web_dir, "index.html"))

    return app


# ── Serializers ───────────────────────────────────────────────────────────────

def _device_summary(device, store):
    ifaces = store.get_interfaces(device.hostname)
    vlans  = store.get_vlans(device=device.hostname)
    return {
        "hostname":        device.hostname,
        "ip":              device.ip,
        "device_type":     device.device_type,
        "model":           device.model,
        "last_seen":       device.last_seen.isoformat() if device.last_seen else None,
        "tags":            device.tags,
        "interface_count": len(ifaces),
        "vlan_count":      len(vlans),
        "trunk_count":     sum(1 for i in ifaces if i.mode.value == "trunk"),
    }


def _iface_to_dict(iface):
    return {
        "device":      iface.device,
        "name":        iface.name,
        "description": iface.description,
        "mode":        iface.mode.value,
        "status":      iface.status.value,
        "access_vlan": iface.access_vlan,
        "trunk_vlans": iface.trunk_vlans,
        "native_vlan": iface.native_vlan,
        "ip_address":  iface.ip_address,
        "speed":       iface.speed,
    }


def _vlan_to_dict(vlan):
    return {
        "vlan_id":    vlan.vlan_id,
        "name":       vlan.name,
        "status":     vlan.status.value,
        "device":     vlan.device,
        "discovered": vlan.discovered.isoformat() if vlan.discovered else None,
    }


def _job_to_dict(job):
    return {
        "job_id":       job.job_id,
        "vlan_id":      job.request.vlan_id,
        "vlan_name":    job.request.name,
        "switches":     job.request.switches,
        "status":       job.status.value,
        "steps_total":  len(job.steps),
        "steps_ok":     job.steps_succeeded,
        "steps_failed": job.steps_failed,
        "created_at":   job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error":        job.error,
        "steps": [
            {
                "device":    s.device,
                "operation": s.operation.value,
                "interface": s.interface,
                "vlan_id":   s.vlan_id,
                "command":   s.command,
                "success":   s.success,
                "error":     s.error,
                "timestamp": s.timestamp.isoformat() if s.timestamp else None,
            }
            for s in job.steps
        ],
    }
