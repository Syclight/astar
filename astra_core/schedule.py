"""Run a business project on a timetable through the operating system's own scheduler.

Windows gets a Task Scheduler entry (schtasks); Linux and macOS get a cron line to add. The job runs
`astra run --input <saved task> --yes`, so task data is confirmed automatically, while cleanup file
lists and human reviews still wait for a person. Output is appended to a log beside the project.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from astra_core.project import resolve_project_file

DAYS = {'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'}


def on_windows():
    return os.name == 'nt'


def job_name(project_file, name=None):
    """The task name: the one given, or the folder name plus a short tag of the full path, so two projects
    that share a folder name never replace each other's task."""
    import hashlib
    if not name:
        tag = hashlib.sha256(str(Path(project_file).resolve()).casefold().encode('utf-8')).hexdigest()[:6]
        name = f'{Path(project_file).parent.name}_{tag}'
    return re.sub(r'[^0-9A-Za-z_\-\u4e00-\u9fff]+', '_', name).strip('_') or 'astra_job'


def plan(project, *, daily=None, weekly=None, at=None, hourly=None, input_file=None, name=None):
    """Everything needed to register the job, without registering it."""
    project_file = resolve_project_file(project)
    if not project_file.is_file():
        raise ValueError(f'找不到项目文件：{project_file}')
    chosen = [value for value in (daily, weekly, hourly) if value]
    if len(chosen) != 1:
        raise ValueError('请只选一种频率：--daily HH:MM、--weekly MON,FRI --at HH:MM 或 --hourly N')
    time = daily or at
    if (daily or weekly) and not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', time or ''):
        raise ValueError('时间写成 24 小时制 HH:MM，例如 08:30')
    days = []
    if weekly:
        days = [day.strip().upper()[:3] for day in weekly.split(',') if day.strip()]
        if not days or not set(days) <= DAYS:
            raise ValueError('星期写成 MON,TUE,WED,THU,FRI,SAT,SUN 中的一个或多个，用逗号分隔')
    if hourly and not 1 <= int(hourly) <= 23:
        raise ValueError('--hourly 取 1–23')
    base = project_file.parent
    name = job_name(project_file, name)
    folder = base / 'schedules'
    saved_input = None
    if input_file:
        source = Path(input_file).resolve()
        if not source.is_file():
            raise ValueError(f'找不到任务资料文件：{source}')
        saved_input = folder / f'{name}.json'  # a copy, so later edits to the original do not change the job
    windows = on_windows()
    script = folder / (f'{name}.cmd' if windows else f'{name}.sh')
    log = folder / f'{name}.log'
    command = [sys.executable, '-m', 'astra_core.cli', 'run', str(project_file), '--yes']
    if saved_input:
        command[5:5] = ['--input', str(saved_input)]
    return {'name': name, 'project': str(project_file), 'folder': folder, 'script': script, 'log': log,
            'input': saved_input, 'source_input': Path(input_file).resolve() if input_file else None,
            'command': command, 'windows': windows, 'daily': daily, 'weekly': days, 'at': time, 'hourly': hourly}


def script_text(job):
    if job['windows']:
        # cmd expands %...% even inside quotes, so a literal % in a path is written as %%.
        escape = lambda value: '"' + str(value).replace('%', '%%') + '"'
        command = ' '.join(escape(part) for part in job['command'])
        return (f'@echo off\r\nchcp 65001 >nul\r\ncd /d {escape(Path(job["project"]).parent)}\r\n'
                f'echo ==== %date% %time% ==== >> {escape(job["log"])}\r\n{command} >> {escape(job["log"])} 2>&1\r\n')
    import shlex
    command = ' '.join(shlex.quote(str(part)) for part in job['command'])
    log = shlex.quote(str(job['log']))
    return (f'#!/bin/sh\ncd {shlex.quote(str(Path(job["project"]).parent))} || exit 1\n'
            f'echo "==== $(date) ====" >> {log}\n{command} >> {log} 2>&1\n')


def scheduler_command(job):
    if job['windows']:
        command = ['schtasks', '/Create', '/F', '/TN', f'Astra\\{job["name"]}', '/TR', f'"{job["script"]}"']
        if job['hourly']:
            return command + ['/SC', 'HOURLY', '/MO', str(int(job['hourly']))]
        if job['weekly']:
            return command + ['/SC', 'WEEKLY', '/D', ','.join(job['weekly']), '/ST', job['at']]
        return command + ['/SC', 'DAILY', '/ST', job['daily']]
    return None


def cron_line(job):
    if job['hourly']:
        timing = f'0 */{int(job["hourly"])} * * *'
    else:
        hour, minute = job['at'].split(':')
        days = ','.join(job['weekly']) if job['weekly'] else '*'
        timing = f'{int(minute)} {int(hour)} * * {days}'
    import shlex
    return f'{timing} {shlex.quote(str(job["script"]))}'


def create(job, *, register=True, runner=subprocess.run):
    job['folder'].mkdir(parents=True, exist_ok=True)
    if job['input']:
        shutil.copyfile(job['source_input'], job['input'])
    job['script'].write_text(script_text(job), encoding='utf-8', newline='')
    if not job['windows']:
        job['script'].chmod(0o755)
    command = scheduler_command(job)
    if command is None:
        return {'registered': False, 'cron': cron_line(job)}
    if not register:
        return {'registered': False, 'command': command}
    done = runner(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError('创建计划任务失败：' + (done.stderr or done.stdout or '').strip())
    return {'registered': True, 'command': command}


def remove(name, *, runner=subprocess.run):
    if not on_windows():
        return {'removed': False, 'hint': '用 crontab -e 删除对应的那一行'}
    done = runner(['schtasks', '/Delete', '/F', '/TN', f'Astra\\{name}'], capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError('删除计划任务失败：' + (done.stderr or done.stdout or '').strip())
    return {'removed': True}
