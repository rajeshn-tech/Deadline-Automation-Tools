# -*- coding: utf-8 -*-


from __future__ import print_function

import os
import sys
import csv
import traceback
from datetime import datetime, date, timedelta
from collections import defaultdict

# -----------------------------------------------------------------------------
# DEADLINE IMPORTS
# -----------------------------------------------------------------------------
try:
    from Deadline.Scripting import RepositoryUtils, ClientUtils
except Exception:
    RepositoryUtils = None
    ClientUtils = None

try:
    from DeadlineUI.Controls.Scripting.DeadlineScriptDialog import DeadlineScriptDialog
except Exception:
    DeadlineScriptDialog = None


# =============================================================================
# CONFIGURATION — FALLBACK OUTPUT FOLDER
# =============================================================================
# Fallback directory if no folder has been chosen or saved yet.
OUTPUT_DIR = r"REPLACE_WITH_YOUR_DEADLINE_REPOSITORY_REPORT_FOLDER"

REPORT_PREFIX = "Deadline_Render_Summary"

# Primary Pool to Department Mapping (Strictly lowercase keys)
POOL_TO_DEPARTMENT = {
    "2d": "Comp",
    "depth": "Depth",
    "paint": "Paint",
    "eleqc": "Roto",
    "dataio": "DataIO",
}

# Sub-mapping for 'dataio' pool based on username (all usernames lowercase)
DATAIO_USER_MAPPING = {
    "AI": ["rakeshn"],
    "Coordinator Review Packages": ["maheshj", "ameyp", "vishalr", "vishalp"],
}

# Fixed order for report display
DEPARTMENT_ORDER = [
    "Comp", "Depth", "Paint", "Roto", "DataIO", "AI", "Coordinator Review Packages"
]

SUMMARY_COLUMNS = [
    "Department", "Received Jobs", "Completed", "Suspended", "Failed", "Pending"
]

DETAIL_COLUMNS = [
    "Job Name", "User", "Task Progress", "Status", "Submit Date/Time",
    "Start Date/Time", "Finished Date/Time", "Clock Running Time", "Pool", "Frames"
]


# =============================================================================
# SAFE DATA HELPERS
# =============================================================================

def safe_get(obj, name, default=None):
    """Safely retrieve an attribute or property from a Deadline .NET/Python object."""
    try:
        val = getattr(obj, name)
        return default if val is None else val
    except Exception:
        return default


def safe_int(value, default=0):
    """Safely convert any value to integer."""
    try:
        return int(value)
    except Exception:
        return default


def as_text(value):
    """Safe string representation compatible with Python 2.7 and Python 3+."""
    if value is None:
        return ""
    try:
        if sys.version_info[0] < 3:
            if isinstance(value, unicode):
                return value.encode("utf-8")
            return str(value)
        return str(value)
    except Exception:
        return ""


def parse_deadline_datetime(value):
    """
    Safely parse Deadline datetime representations.
    Handles both .NET System.DateTime objects and standard string timestamps
    without triggering .NET DateTime.op_Equality type mismatch errors.
    """
    if value is None:
        return None

    # Step 1: Check if it is a .NET System.DateTime or Python datetime object FIRST
    # (Do NOT compare value == "" here, as .NET DateTime crashes on string equality)
    if hasattr(value, "Year"):
        try:
            year = int(value.Year)
            # DateTime.MinValue (uninitialized date) has Year 1 or 0
            if year <= 1:
                return None
            return datetime(
                year,
                int(value.Month),
                int(value.Day),
                int(value.Hour),
                int(value.Minute),
                int(value.Second)
            )
        except Exception:
            return None

    # Step 2: If it is not a DateTime object, convert to text and parse as string
    str_val = as_text(value).strip()
    if not str_val or str_val.startswith("0001-01-01"):
        return None

    date_formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
    ]
    for fmt in date_formats:
        try:
            return datetime.strptime(str_val.split(".")[0], fmt.split(".")[0])
        except Exception:
            continue

    return None


def format_dt(dt):
    """Format Python datetime object to string YYYY-MM-DD HH:MM:SS."""
    if not dt:
        return ""
    try:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return as_text(dt)


def get_task_progress(job):
    """Calculate task progress string safely handling zero task counts."""
    completed = safe_int(safe_get(job, "JobCompletedTasks", 0), 0)
    total = safe_int(safe_get(job, "JobTaskCount", 0), 0)
    if total <= 0:
        return "0/0 (0%)"
    pct = int(round((float(completed) / float(total)) * 100))
    return "{0}/{1} ({2}%)".format(completed, total, pct)


def get_clock_running_time(job):
    """
    Extracts the actual Deadline job runtime.
    Falls back gracefully to wall-clock calculation if attribute is not directly exposed.
    """
    # 1. Check for any direct property or runtime field
    for attr in ("JobClockTime", "ClockRunningTime", "JobTotalRenderTime", "JobRealRenderTime"):
        val = safe_get(job, attr, None)
        if val:
            return as_text(val)

    # 2. Derive elapsed time from Start and Finished timestamps safely
    start_dt = parse_deadline_datetime(safe_get(job, "JobStartedDateTime", None))
    if not start_dt:
        return "00:00:00"

    finish_dt = parse_deadline_datetime(safe_get(job, "JobCompletedDateTime", None))
    if not finish_dt:
        # If job is actively rendering, calculate up to now
        status = as_text(safe_get(job, "JobStatus", "")).lower()
        if "render" in status or "active" in status:
            finish_dt = datetime.now()
        else:
            return "00:00:00"

    diff = finish_dt - start_dt
    total_seconds = int(diff.total_seconds())
    if total_seconds < 0:
        return "00:00:00"

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)

    if days > 0:
        return "{0}.{1:02d}:{2:02d}:{3:02d}".format(days, hours, mins, secs)
    else:
        return "{0:02d}:{1:02d}:{2:02d}".format(hours, mins, secs)


def categorize_status(status_str):
    """
    Maps Deadline internal job statuses to the 4 target summary categories:
    Completed, Suspended, Failed, or Pending.
    """
    st = as_text(status_str).strip().lower()
    if st == "completed":
        return "Completed"
    elif st == "suspended":
        return "Suspended"
    elif st == "failed":
        return "Failed"
    else:
        # Rendering, Queued, Pending, Waiting, or any active status count under Pending
        return "Pending"


def resolve_department(pool_name, user_name):
    """
    Resolves department with special handling for 'dataio' pool sub-routing:
    - AI: rakeshn
    - Coordinator Review Packages: maheshj, ameyp, vishalr, vishalp
    - All other dataio users: DataIO
    - Other pools map via POOL_TO_DEPARTMENT
    """
    pool_key = pool_name.lower().strip()
    user_key = user_name.lower().strip()

    if pool_key == "dataio":
        for dept_name, users in DATAIO_USER_MAPPING.items():
            if user_key in users:
                return dept_name
        return "DataIO"

    return POOL_TO_DEPARTMENT.get(pool_key, None)


def get_settings_filename():
    """
    Returns the user's settings file path for remembering UI defaults across sessions.
    Uses Deadline's ClientUtils.GetUsersSettingsDirectory() with fallback to user home directory.
    """
    try:
        if ClientUtils is not None and hasattr(ClientUtils, "GetUsersSettingsDirectory"):
            settings_dir = ClientUtils.GetUsersSettingsDirectory()
            if settings_dir and os.path.exists(settings_dir):
                return os.path.join(settings_dir, "RenderSummaryGeneratorSettings.ini")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "RenderSummaryGeneratorSettings.ini")


# =============================================================================
# CORE REPORT GENERATOR ENGINE
# =============================================================================

def generate_report(start_date_obj, end_date_obj, output_directory):
    """
    High performance single-pass job analyzer and CSV generator.
    """
    if RepositoryUtils is None:
        raise RuntimeError("Deadline RepositoryUtils API is not available.")

    # 1. Fetch all non-deleted jobs from Deadline (Single Repository Query)
    all_jobs = RepositoryUtils.GetJobs(False)
    if all_jobs is None:
        all_jobs = []

    # 2. Single-pass loop:
    #    a) Build user historical pool usage (for fallback).
    #    b) Filter jobs falling within [start_date, end_date] by Submit Date.
    user_pool_counts = defaultdict(lambda: defaultdict(int))
    matched_jobs = []

    for job in all_jobs:
        user_name = as_text(safe_get(job, "JobUserName", "")).strip().lower()
        pool_name = as_text(safe_get(job, "JobPool", "")).strip().lower()

        # If pool/user is recognized, record this user's historical department usage
        dept = resolve_department(pool_name, user_name)
        if dept:
            user_pool_counts[user_name][dept] += 1

        # Check Submit Date
        submit_dt = parse_deadline_datetime(safe_get(job, "JobSubmitDateTime", None))
        if submit_dt:
            job_date = submit_dt.date()
            if start_date_obj <= job_date <= end_date_obj:
                matched_jobs.append((job, submit_dt))

    # 3. Department Determination & Metrics Aggregation
    # Structure: summary_data[dept] = {"Received": 0, "Completed": 0, "Suspended": 0, "Failed": 0, "Pending": 0}
    summary_data = {
        dept: {"Received": 0, "Completed": 0, "Suspended": 0, "Failed": 0, "Pending": 0}
        for dept in DEPARTMENT_ORDER
    }
    summary_data["None"] = {"Received": 0, "Completed": 0, "Suspended": 0, "Failed": 0, "Pending": 0}

    detail_rows = []

    for job, submit_dt in matched_jobs:
        user_name = as_text(safe_get(job, "JobUserName", "")).strip()
        user_key = user_name.lower()
        pool_raw = as_text(safe_get(job, "JobPool", "")).strip()
        pool_key = pool_raw.lower()

        # Department classification:
        dept = resolve_department(pool_key, user_key)
        if dept:
            assigned_dept = dept
        else:
            # Fallback to user history
            user_history = user_pool_counts.get(user_key, {})
            if user_history:
                # Deterministic tie-breaking: Sort by (-count, department_name)
                sorted_depts = sorted(
                    user_history.items(),
                    key=lambda item: (-item[1], item[0])
                )
                assigned_dept = sorted_depts[0][0]
            else:
                assigned_dept = "None"

        # Categorize status
        raw_status = as_text(safe_get(job, "JobStatus", "Unknown")).strip()
        category = categorize_status(raw_status)

        # Increment counts
        summary_data[assigned_dept]["Received"] += 1
        summary_data[assigned_dept][category] += 1

        # Extract job detail row
        job_name = as_text(safe_get(job, "JobName", ""))
        progress_str = get_task_progress(job)
        start_dt = parse_deadline_datetime(safe_get(job, "JobStartedDateTime", None))
        finished_dt = parse_deadline_datetime(safe_get(job, "JobCompletedDateTime", None))
        clock_time_str = get_clock_running_time(job)
        frames_val = as_text(safe_get(job, "JobFrames", ""))

        # Prevent Excel auto-converting frame ranges (e.g. 1-50 to Jan-50)
        if frames_val:
            frames_val = '="{0}"'.format(frames_val)

        detail_rows.append([
            job_name,
            user_name,
            progress_str,
            raw_status,
            format_dt(submit_dt),
            format_dt(start_dt),
            format_dt(finished_dt),
            clock_time_str,
            pool_raw,
            frames_val
        ])

    # Sort detail rows by submit date ascending
    detail_rows.sort(key=lambda row: row[4])

    # 4. Export to CSV (UTF-8 BOM Excel-friendly format)
    if not os.path.exists(output_directory):
        try:
            os.makedirs(output_directory)
        except Exception as e:
            raise IOError("Could not create output directory '{0}': {1}".format(output_directory, str(e)))

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    start_tag = start_date_obj.strftime("%Y%m%d")
    end_tag = end_date_obj.strftime("%Y%m%d")
    filename = "{0}_{1}_to_{2}_{3}.csv".format(REPORT_PREFIX, start_tag, end_tag, timestamp_str)
    output_filepath = os.path.join(output_directory, filename)

    write_csv_report(
        output_filepath,
        start_date_obj,
        end_date_obj,
        summary_data,
        detail_rows
    )

    return output_filepath, len(matched_jobs)


def write_csv_report(filepath, start_date_obj, end_date_obj, summary_data, detail_rows):
    """
    Writes the consolidated report:
    - Top: Period Summary Block
    - Middle: Separator
    - Bottom: Individual Job Details
    """
    # Open file with UTF-8 BOM encoding for Excel compatibility across Python 2 & 3
    if sys.version_info[0] >= 3:
        f = open(filepath, "w", newline="", encoding="utf-8-sig")
    else:
        f = open(filepath, "wb")
        f.write(b"\xef\xbb\xbf")  # UTF-8 BOM

    try:
        writer = csv.writer(f)

        # Title and metadata headers
        writer.writerow(["DEADLINE RENDER SUMMARY REPORT"])
        writer.writerow(["Period", "{0} to {1}".format(start_date_obj.strftime("%Y-%m-%d"), end_date_obj.strftime("%Y-%m-%d"))])
        writer.writerow(["Generated At", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        writer.writerow([])

        # --- SECTION 1: CONSOLIDATED DEPARTMENT SUMMARY ---
        writer.writerow(["--- CONSOLIDATED DEPARTMENT SUMMARY ---"])
        writer.writerow(SUMMARY_COLUMNS)

        total_received = 0
        total_completed = 0
        total_suspended = 0
        total_failed = 0
        total_pending = 0

        # Output standard departments
        for dept in DEPARTMENT_ORDER:
            d = summary_data[dept]
            writer.writerow([
                dept,
                d["Received"],
                d["Completed"],
                d["Suspended"],
                d["Failed"],
                d["Pending"]
            ])
            total_received += d["Received"]
            total_completed += d["Completed"]
            total_suspended += d["Suspended"]
            total_failed += d["Failed"]
            total_pending += d["Pending"]

        # Output 'None' department row if it has any jobs
        d_none = summary_data["None"]
        if d_none["Received"] > 0:
            writer.writerow([
                "None",
                d_none["Received"],
                d_none["Completed"],
                d_none["Suspended"],
                d_none["Failed"],
                d_none["Pending"]
            ])
            total_received += d_none["Received"]
            total_completed += d_none["Completed"]
            total_suspended += d_none["Suspended"]
            total_failed += d_none["Failed"]
            total_pending += d_none["Pending"]

        # Total summary row
        writer.writerow([
            "Total",
            total_received,
            total_completed,
            total_suspended,
            total_failed,
            total_pending
        ])

        # Blank separator rows
        writer.writerow([])
        writer.writerow([])

        # --- SECTION 2: INDIVIDUAL JOB DETAILS ---
        writer.writerow(["--- INDIVIDUAL JOB DETAILS ---"])
        writer.writerow(DETAIL_COLUMNS)

        for row in detail_rows:
            writer.writerow(row)

    finally:
        f.close()


# =============================================================================
# DEADLINE MONITOR UI DIALOG
# =============================================================================

scriptDialog = None


def OnGeneratePressed(*args):
    global scriptDialog

    start_str = scriptDialog.GetValue("StartDateBox").strip()
    end_str = scriptDialog.GetValue("EndDateBox").strip()
    target_dir = scriptDialog.GetValue("OutputFolderBox").strip()

    # Validate dates
    try:
        start_date_obj = datetime.strptime(start_str, "%Y-%m-%d").date()
    except ValueError:
        scriptDialog.ShowMessageBox(
            "Invalid Start Date format. Please use YYYY-MM-DD (e.g. 2026-10-08).",
            "Date Format Error"
        )
        return

    try:
        end_date_obj = datetime.strptime(end_str, "%Y-%m-%d").date()
    except ValueError:
        scriptDialog.ShowMessageBox(
            "Invalid End Date format. Please use YYYY-MM-DD (e.g. 2026-10-14).",
            "Date Format Error"
        )
        return

    if start_date_obj > end_date_obj:
        scriptDialog.ShowMessageBox(
            "Start Date cannot be after End Date.",
            "Date Range Error"
        )
        return

    # Validate Output Directory
    if not target_dir or "REPLACE_WITH_YOUR" in target_dir:
        scriptDialog.ShowMessageBox(
            "Please select a valid Output Directory before generating the report.",
            "Output Directory Missing"
        )
        return

    # Save as default settings if checkbox is ticked
    if scriptDialog.GetValue("SetDefaultBox"):
        try:
            settings_file = get_settings_filename()
            scriptDialog.SaveSettings(settings_file, ["OutputFolderBox"])
        except Exception:
            pass

    # Run generator
    try:
        output_path, job_count = generate_report(start_date_obj, end_date_obj, target_dir)
        scriptDialog.CloseDialog()

        success_msg = (
            "Render Summary Report generated successfully!\n\n"
            "Date Range: {0} to {1}\n"
            "Jobs Processed: {2}\n\n"
            "Saved to:\n{3}"
        ).format(start_str, end_str, job_count, output_path)

        scriptDialog.ShowMessageBox(success_msg, "Report Generated")

    except Exception as ex:
        err_msg = "An error occurred while generating the report:\n\n{0}\n\n{1}".format(
            str(ex), traceback.format_exc()
        )
        scriptDialog.ShowMessageBox(err_msg, "Execution Error")


def OnCancelPressed(*args):
    global scriptDialog
    if scriptDialog:
        scriptDialog.CloseDialog()


def __main__(*args):
    """Entry point when executed from Deadline Monitor Scripts menu."""
    global scriptDialog

    if DeadlineScriptDialog is None:
        print("Error: Deadline UI environment is not loaded.")
        return

    # Default date range: Last 7 days up to today
    today_dt = date.today()
    default_start = (today_dt - timedelta(days=6)).strftime("%Y-%m-%d")
    default_end = today_dt.strftime("%Y-%m-%d")

    # Initial output directory fallback
    initial_output_dir = OUTPUT_DIR
    if not initial_output_dir or "REPLACE_WITH_YOUR" in initial_output_dir:
        initial_output_dir = os.path.join(os.path.expanduser("~"), "Desktop")

    scriptDialog = DeadlineScriptDialog()
    scriptDialog.SetSize(480, 200)
    scriptDialog.AllowResizingDialog(False)
    scriptDialog.SetTitle("Deadline Render Summary Generator")

    # Grid 1: Date Range & Output Directory Inputs
    scriptDialog.AddGrid()
    scriptDialog.AddControlToGrid("StartLabel", "LabelControl", "Start Date (YYYY-MM-DD):", 0, 0, "Inclusive Start Date", False)
    scriptDialog.AddControlToGrid("StartDateBox", "TextControl", default_start, 0, 1)

    scriptDialog.AddControlToGrid("EndLabel", "LabelControl", "End Date (YYYY-MM-DD):", 1, 0, "Inclusive End Date", False)
    scriptDialog.AddControlToGrid("EndDateBox", "TextControl", default_end, 1, 1)

    scriptDialog.AddControlToGrid("OutputFolderLabel", "LabelControl", "Output Directory:", 2, 0, "Folder where CSV report will be saved", False)
    scriptDialog.AddSelectionControlToGrid("OutputFolderBox", "FolderBrowserControl", initial_output_dir, "", 2, 1)

    scriptDialog.AddSelectionControlToGrid("SetDefaultBox", "CheckBoxControl", False, "Set as default output directory", 3, 1, "Save this path as default for future reports")
    scriptDialog.EndGrid()

    # Load persistent settings if available
    settings_file = get_settings_filename()
    if os.path.exists(settings_file):
        try:
            scriptDialog.LoadSettings(settings_file, ["OutputFolderBox"])
        except Exception:
            pass

    # Grid 2: Action Buttons
    scriptDialog.AddGrid()
    gen_btn = scriptDialog.AddControlToGrid("GenerateBtn", "ButtonControl", "Generate Report", 0, 0, expand=False)
    gen_btn.ValueModified.connect(OnGeneratePressed)

    cancel_btn = scriptDialog.AddControlToGrid("CancelBtn", "ButtonControl", "Cancel", 0, 1, expand=False)
    cancel_btn.ValueModified.connect(OnCancelPressed)
    scriptDialog.EndGrid()

    # Show modal dialog
    scriptDialog.ShowDialog(True)


if __name__ == "__main__":
    __main__()
