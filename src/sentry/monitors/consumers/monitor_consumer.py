import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, Mapping, Optional, TypedDict

import msgpack
import sentry_sdk
from arroyo.backends.kafka.consumer import KafkaPayload
from arroyo.processing.strategies.abstract import ProcessingStrategy, ProcessingStrategyFactory
from arroyo.processing.strategies.commit import CommitOffsets
from arroyo.processing.strategies.run_task import RunTask
from arroyo.types import BrokerValue, Commit, Message, Partition
from django.conf import settings
from django.db import router, transaction
from django.utils.text import slugify
from typing_extensions import NotRequired

from sentry import ratelimits
from sentry.constants import ObjectStatus
from sentry.killswitches import killswitch_matches_context
from sentry.models import Project
from sentry.monitors.models import (
    MAX_SLUG_LENGTH,
    CheckInStatus,
    Monitor,
    MonitorCheckIn,
    MonitorEnvironment,
    MonitorEnvironmentLimitsExceeded,
    MonitorEnvironmentValidationFailed,
    MonitorLimitsExceeded,
    MonitorType,
)
from sentry.monitors.utils import (
    get_new_timeout_at,
    get_timeout_at,
    signal_first_checkin,
    signal_first_monitor_created,
    valid_duration,
)
from sentry.monitors.validators import ConfigValidator, MonitorCheckInValidator
from sentry.utils import json, metrics, redis
from sentry.utils.dates import to_datetime
from sentry.utils.locking import UnableToAcquireLock
from sentry.utils.locking.manager import LockManager
from sentry.utils.services import build_instance_from_options

locks = LockManager(build_instance_from_options(settings.SENTRY_POST_PROCESS_LOCKS_BACKEND_OPTIONS))

logger = logging.getLogger(__name__)

CHECKIN_QUOTA_LIMIT = 5
CHECKIN_QUOTA_WINDOW = 60

# This key is used when SENTRY_MONITORS_HIGH_VOLUME_MODE is enabled and we
# trigger the monitor tasks as a side-effect of check-ins coming in. It is used
# to store he last timestamp that the tasks were triggered.
HIGH_VOLUME_LAST_TRIGGER_TS_KEY = "sentry.monitors.last_tasks_ts"


class CheckinMessage(TypedDict):
    payload: str
    start_time: str
    project_id: str
    sdk: str


class CheckinTrace(TypedDict):
    trace_id: str


class CheckinContexts(TypedDict):
    trace: NotRequired[CheckinTrace]


class CheckinPayload(TypedDict):
    check_in_id: str
    monitor_slug: str
    status: str
    environment: NotRequired[str]
    duration: NotRequired[int]
    monitor_config: NotRequired[Dict]
    contexts: NotRequired[CheckinContexts]


def _ensure_monitor_with_config(
    project: Project,
    monitor_slug: str,
    monitor_slug_from_param: str,
    config: Optional[Dict],
):
    try:
        monitor = Monitor.objects.get(
            slug=monitor_slug,
            project_id=project.id,
            organization_id=project.organization_id,
        )
    except Monitor.DoesNotExist:
        monitor = None

    # XXX(epurkhiser): Temporary dual-read logic to handle some monitors
    # that were created before we correctly slugified slugs on upsert in
    # this consumer.
    #
    # Once all slugs are correctly slugified we can remove this.
    if not monitor:
        try:
            monitor = Monitor.objects.get(
                slug=monitor_slug_from_param,
                project_id=project.id,
                organization_id=project.organization_id,
            )
        except Monitor.DoesNotExist:
            pass

    if not config:
        return monitor

    validator = ConfigValidator(data=config)

    if not validator.is_valid():
        logger.debug(f"invalid monitor_config: {monitor_slug}")
        return monitor

    validated_config = validator.validated_data
    created = False

    # Create monitor
    if not monitor:
        monitor, created = Monitor.objects.update_or_create(
            organization_id=project.organization_id,
            slug=monitor_slug,
            defaults={
                "project_id": project.id,
                "name": monitor_slug,
                "status": ObjectStatus.ACTIVE,
                "type": MonitorType.CRON_JOB,
                "config": validated_config,
            },
        )
        signal_first_monitor_created(project, None, True)

    # Update existing monitor
    if monitor and not created and monitor.config != validated_config:
        monitor.update_config(config, validated_config)

    return monitor


def _dispatch_tasks(ts: datetime):
    # For now we're going to have this do nothing. We want to validate that
    # we're not going to be skipping any check-ins
    return

    # check_missing.delay(current_datetime=ts)
    # check_timeout.delay(current_datetime=ts)


def _handle_clock_pulse_task_trigger(ts: datetime):
    """
    Handles clock pulse messages. These pulses are generated by the
    `sentry.monitors.tasks.clock_pulse` tasks which runs every minute. Clock
    pulses will NOT be generated when SENTRY_MONITORS_HIGH_VOLUME_MODE is
    enabled.

    This function is responsible for dispatching the missed check-in and timed
    out check-in detection tasks.
    """
    _dispatch_tasks(ts)


def _try_handle_high_volume_task_trigger(ts: datetime):
    """
    When SENTRY_MONITORS_HIGH_VOLUME_MODE is enabled we use each check-in
    message as a pseudo clock.
    """
    redis_client = redis.redis_clusters.get(settings.SENTRY_MONITORS_REDIS_CLUSTER)

    # Trim the timestamp seconds off, these tasks are run once per minute and
    # should have their timestamp clamped to the minute.
    reference_datetime = ts.replace(second=0, microsecond=0)
    reference_ts = int(reference_datetime.timestamp())

    last_ts = redis_client.get(HIGH_VOLUME_LAST_TRIGGER_TS_KEY)
    if last_ts is not None:
        last_ts = int(last_ts)

    # Do nothing until the message we process moves across the minute boundary
    if last_ts == reference_ts:
        return

    try:
        lock = locks.get("sentry.monitors.task_trigger", duration=5)
        with lock.acquire():
            # Track the delay from the true time, ideally this should be pretty
            # close, but in the case of a backlog, this will be much higher
            total_delay = reference_ts - datetime.now().timestamp()

            metrics.incr("monitors.task.triggered_via_high_volume_clock")
            metrics.gauge("monitors.task.high_volume_clock_delay", total_delay)

            # If more than exactly a minute has passed then we've skipped a
            # task run, report that to sentry, it is a problem.
            if last_ts is not None and last_ts + 60 != reference_ts:
                with sentry_sdk.push_scope() as scope:
                    scope.set_extra("last_ts", last_ts)
                    scope.set_extra("reference_ts", reference_ts)
                    sentry_sdk.capture_message("Monitor task dispatch minute skipped")

            _dispatch_tasks(ts)
            redis_client.set(HIGH_VOLUME_LAST_TRIGGER_TS_KEY, reference_ts)
    except UnableToAcquireLock:
        # Another message processor is handling this. Nothing to do
        pass


def _process_message(ts: datetime, wrapper: CheckinMessage) -> None:
    # When running in high volume mode we will not consume clock pulses (The
    # clock_pulse task is not enabled). Instead we use each check-in message as
    # a means for triggering our tasks.
    if settings.SENTRY_MONITORS_HIGH_VOLUME_MODE:
        try:
            _try_handle_high_volume_task_trigger(ts)
        except Exception:
            logger.exception("Failed try high-volume task trigger", exc_info=True)

    params: CheckinPayload = json.loads(wrapper["payload"])
    start_time = to_datetime(float(wrapper["start_time"]))
    project_id = int(wrapper["project_id"])
    source_sdk = wrapper["sdk"]

    # Ensure the monitor_slug is slugified, since we are not running this
    # through the MonitorValidator we must do this here.
    monitor_slug = slugify(params["monitor_slug"])[:MAX_SLUG_LENGTH].strip("-")

    environment = params.get("environment")
    project = Project.objects.get_from_cache(id=project_id)

    ratelimit_key = f"{project.organization_id}:{monitor_slug}:{environment}"

    metric_kwargs = {
        "source": "consumer",
        "source_sdk": source_sdk,
    }

    if killswitch_matches_context(
        "crons.organization.disable-check-in", {"organization_id": project.organization_id}
    ):
        metrics.incr(
            "monitors.checkin.dropped.blocked",
            tags={**metric_kwargs},
        )
        logger.debug(
            f"monitor check in blocked via killswitch: {project.organization_id} - {monitor_slug}"
        )
        return

    if ratelimits.is_limited(
        f"monitor-checkins:{ratelimit_key}",
        limit=CHECKIN_QUOTA_LIMIT,
        window=CHECKIN_QUOTA_WINDOW,
    ):
        metrics.incr(
            "monitors.checkin.dropped.ratelimited",
            tags={**metric_kwargs},
        )
        logger.debug(f"monitor check in rate limited: {monitor_slug}")
        return

    def update_existing_check_in(
        existing_check_in: MonitorCheckIn,
        updated_status: CheckInStatus,
        updated_duration: float,
        new_date_updated: datetime,
    ):
        if (
            existing_check_in.project_id != project_id
            or existing_check_in.monitor_id != monitor.id
            or existing_check_in.monitor_environment_id != monitor_environment.id
        ):
            metrics.incr(
                "monitors.checkin.result",
                tags={"source": "consumer", "status": "guid_mismatch"},
            )
            logger.debug(
                f"check-in guid {existing_check_in} already associated with {existing_check_in.monitor_id} not payload monitor {monitor.id}"
            )
            return

        if existing_check_in.status in CheckInStatus.FINISHED_VALUES:
            metrics.incr(
                "monitors.checkin.result",
                tags={**metric_kwargs, "status": "checkin_finished"},
            )
            logger.debug(
                f"check-in was finished: attempted update from {existing_check_in.status} to {updated_status}"
            )
            return

        if updated_duration is None:
            updated_duration = int(
                (start_time - existing_check_in.date_added).total_seconds() * 1000
            )

        if not valid_duration(updated_duration):
            metrics.incr(
                "monitors.checkin.result",
                tags={**metric_kwargs, "status": "failed_duration_check"},
            )
            logger.debug(f"check-in implicit duration is invalid: {updated_duration}")
            return

        # update date_added for heartbeat
        date_updated = existing_check_in.date_updated
        if updated_status == CheckInStatus.IN_PROGRESS:
            date_updated = new_date_updated

        updated_timeout_at = get_new_timeout_at(existing_check_in, updated_status, new_date_updated)

        existing_check_in.update(
            status=updated_status,
            duration=updated_duration,
            date_updated=date_updated,
            timeout_at=updated_timeout_at,
        )

        return

    try:
        check_in_id = uuid.UUID(params["check_in_id"])
    except ValueError:
        metrics.incr(
            "monitors.checkin.result",
            tags={**metric_kwargs, "status": "failed_guid_validation"},
        )
        logger.info("monitor_checkin.validation.failed", extra={**params})
        return

    # When the UUID is empty we will default to looking for the most
    # recent check-in which is not in a terminal state.
    use_latest_checkin = check_in_id.int == 0

    # If the UUID is unset (zero value) generate a new UUID
    if check_in_id.int == 0:
        guid = uuid.uuid4()
    else:
        guid = check_in_id

    lock = locks.get(f"checkin-creation:{guid}", duration=2, name="checkin_creation")
    try:
        with lock.acquire(), transaction.atomic(router.db_for_write(Monitor)):
            try:
                monitor_config = params.pop("monitor_config", None)

                params["duration"] = (
                    # Duration is specified in seconds from the client, it is
                    # stored in the checkin model as milliseconds
                    int(params["duration"] * 1000)
                    if params.get("duration") is not None
                    else None
                )

                validator = MonitorCheckInValidator(
                    data=params,
                    partial=True,
                    context={
                        "project": project,
                    },
                )

                if not validator.is_valid():
                    metrics.incr(
                        "monitors.checkin.result",
                        tags={**metric_kwargs, "status": "failed_checkin_validation"},
                    )
                    logger.info("monitor_checkin.validation.failed", extra={**params})
                    return

                validated_params = validator.validated_data

                monitor = _ensure_monitor_with_config(
                    project,
                    monitor_slug,
                    params["monitor_slug"],
                    monitor_config,
                )

                if not monitor:
                    metrics.incr(
                        "monitors.checkin.result",
                        tags={**metric_kwargs, "status": "failed_validation"},
                    )
                    logger.info("monitor.validation.failed", extra={**params})
                    return
            except MonitorLimitsExceeded:
                metrics.incr(
                    "monitors.checkin.result",
                    tags={**metric_kwargs, "status": "failed_monitor_limits"},
                )
                logger.debug(f"monitor exceeds limits for organization: {project.organization_id}")
                return

            try:
                monitor_environment = MonitorEnvironment.objects.ensure_environment(
                    project, monitor, environment
                )
            except MonitorEnvironmentLimitsExceeded:
                metrics.incr(
                    "monitors.checkin.result",
                    tags={**metric_kwargs, "status": "failed_monitor_environment_limits"},
                )
                logger.debug(f"monitor environment exceeds limits for monitor: {monitor_slug}")
                return
            except MonitorEnvironmentValidationFailed:
                metrics.incr(
                    "monitors.checkin.result",
                    tags={**metric_kwargs, "status": "failed_monitor_environment_name_length"},
                )
                logger.debug(f"monitor environment name too long: {monitor_slug} - {environment}")
                return

            status = getattr(CheckInStatus, validated_params["status"].upper())
            trace_id = validated_params.get("contexts", {}).get("trace", {}).get("trace_id")

            try:
                if use_latest_checkin:
                    check_in = (
                        MonitorCheckIn.objects.select_for_update()
                        .filter(monitor_environment=monitor_environment)
                        .exclude(status__in=CheckInStatus.FINISHED_VALUES)
                        .order_by("-date_added")[:1]
                        .get()
                    )
                else:
                    check_in = MonitorCheckIn.objects.select_for_update().get(
                        guid=check_in_id,
                    )

                    if check_in.monitor_environment_id != monitor_environment.id:
                        metrics.incr(
                            "monitors.checkin.result",
                            tags={
                                **metric_kwargs,
                                "status": "failed_monitor_environment_guid_match",
                            },
                        )
                        logger.debug(
                            f"monitor environment does not match on existing guid: {environment} - {check_in_id}"
                        )
                        return

                update_existing_check_in(check_in, status, validated_params["duration"], start_time)

            except MonitorCheckIn.DoesNotExist:
                # Infer the original start time of the check-in from the duration.
                # Note that the clock of this worker may be off from what Relay is reporting.
                date_added = start_time
                duration = validated_params["duration"]
                if duration is not None:
                    date_added -= timedelta(milliseconds=duration)

                expected_time = None
                if monitor_environment.last_checkin:
                    expected_time = monitor.get_next_scheduled_checkin(
                        monitor_environment.last_checkin
                    )

                monitor_config = monitor.get_validated_config()
                timeout_at = get_timeout_at(monitor_config, status, date_added)

                check_in, created = MonitorCheckIn.objects.get_or_create(
                    defaults={
                        "duration": duration,
                        "status": status,
                        "date_added": date_added,
                        "date_updated": start_time,
                        "expected_time": expected_time,
                        "timeout_at": timeout_at,
                        "monitor_config": monitor_config,
                        "trace_id": trace_id,
                    },
                    project_id=project_id,
                    monitor=monitor,
                    monitor_environment=monitor_environment,
                    guid=guid,
                )
                if not created:
                    update_existing_check_in(check_in, status, duration, start_time)
                else:
                    signal_first_checkin(project, monitor)

            if check_in.status == CheckInStatus.ERROR:
                monitor_environment.mark_failed(
                    start_time, occurrence_context={"trace_id": trace_id}
                )
            else:
                monitor_environment.mark_ok(check_in, start_time)

            metrics.incr(
                "monitors.checkin.result",
                tags={**metric_kwargs, "status": "complete"},
            )
    except UnableToAcquireLock:
        metrics.incr(
            "monitors.checkin.result",
            tags={**metric_kwargs, "status": "failed_checkin_creation_lock"},
        )
        logger.debug(f"failed to acquire lock to create check-in: {guid}")
    except Exception:
        # Skip this message and continue processing in the consumer.
        metrics.incr(
            "monitors.checkin.result",
            tags={**metric_kwargs, "status": "error"},
        )
        logger.exception("Failed to process check-in", exc_info=True)


class StoreMonitorCheckInStrategyFactory(ProcessingStrategyFactory[KafkaPayload]):
    def create_with_partitions(
        self,
        commit: Commit,
        partitions: Mapping[Partition, int],
    ) -> ProcessingStrategy[KafkaPayload]:
        def process_message(message: Message[KafkaPayload]) -> None:
            assert isinstance(message.value, BrokerValue)
            try:
                wrapper = msgpack.unpackb(message.payload.value)
                _process_message(message.value.timestamp, wrapper)
            except Exception:
                logger.exception("Failed to process message payload")

        return RunTask(
            function=process_message,
            next_step=CommitOffsets(commit),
        )
