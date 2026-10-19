# PriorityBooster.py
# Deadline Monitor Job Script
# Sets selected job(s) to Priority 80 and Machine Limit 0.

from Deadline.Scripting import MonitorUtils, RepositoryUtils
from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon


def __main__(*args):
    jobs = MonitorUtils.GetSelectedJobs()

    if not jobs:
        MessageBox.Show(
            "Select at least one job in Deadline Monitor, then run Priority Booster.",
            "Priority Booster",
            MessageBoxButtons.OK,
            MessageBoxIcon.Information
        )
        return

    succeeded = []
    failed = []

    for job in jobs:
        job_id = str(job.JobId)
        job_name = str(job.JobName)

        try:
            RepositoryUtils.SetMachineLimitMaximum(job_id, 0)
            job.JobPriority = 80
            RepositoryUtils.SaveJob(job)
            succeeded.append("{} ({})".format(job_name, job_id))
        except Exception as exc:
            failed.append("{} ({}): {}".format(job_name, job_id, str(exc)))

    lines = []
    if succeeded:
        lines.append("Updated successfully (Priority 80, Machine Limit 0):")
        lines.extend(succeeded)

    if failed:
        lines.append("")
        lines.append("Failed:")
        lines.extend(failed)

    icon = MessageBoxIcon.Information if not failed else MessageBoxIcon.Warning
    MessageBox.Show("\n".join(lines), "Priority Booster", MessageBoxButtons.OK, icon)
