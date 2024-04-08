import logging
import os
import re
from datetime import datetime
from typing import Any

import dramatiq
import fastpurge
from dramatiq.middleware import CurrentMessage
from sqlalchemy.orm import Session

from exodus_gw import models
from exodus_gw.aws.dynamodb import DynamoDB
from exodus_gw.aws.util import uri_alias
from exodus_gw.database import db_engine
from exodus_gw.schemas import TaskStates
from exodus_gw.settings import Settings, get_environment

LOG = logging.getLogger("exodus-gw")


class Flusher:
    def __init__(
        self,
        paths: list[str],
        settings: Settings,
        env: str,
        cdn_definitions: dict[str, Any],
    ):
        self.paths = paths
        self.settings = settings
        self.cdn_definitions = cdn_definitions

        for environment in settings.environments:
            if environment.name == env:
                self.env = environment

        assert self.env

    @property
    def aliases(self):
        uri_aliases = []
        for k, v in self.cdn_definitions.items():
            if k in ("origin_alias", "releasever_alias", "rhui_alias"):
                uri_aliases.extend(v)
        return uri_aliases

    def arl_ttl(self, path: str):
        # Return an appropriate TTL value for certain paths.
        #
        # Note that this logic has to match the behavior configured at
        # the CDN edge.
        #
        # This logic was originally sourced from rhsm-akamai-cache-purge.

        ttl = "30d"  # default ttl
        ostree_re = r".*/ostree/repo/refs/heads/.*/(base|standard)$"
        if path.endswith(("/repodata/repomd.xml", "/")):
            ttl = "4h"
        elif (
            path.endswith(("/PULP_MANIFEST", "/listing"))
            or ("/repodata/" in path)
            or re.match(ostree_re, path)
        ):
            ttl = "10m"

        return ttl

    @property
    def urls_for_flush(self):
        out: list[str] = []

        paths = set()

        # Use aliases to inflate the paths.
        # e.g. if there is a path of /foo/bar/8/baz and there is an alias
        # of /foo/bar/8 => /foo/bar/8.9, then 'paths' should contain both
        # sides of that alias.
        for path in self.paths:
            # We accept paths both with and without leading '/', normalize.
            path = path.removeprefix("/")

            # This path always goes into the set we'll process.
            paths.add(path)

            # The path after alias resolution also goes into the set.
            # Alias resolution needs the leading '/'.
            path_resolved = uri_alias("/" + path, self.aliases)
            paths.add(path_resolved.removeprefix("/"))

        path_list = sorted(paths)

        for cdn_base_url in self.env.cache_flush_urls:
            for path in path_list:
                out.append(os.path.join(cdn_base_url, path))

        for arl_template in self.env.cache_flush_arl_templates:
            for path in path_list:
                out.append(
                    arl_template.format(
                        path=path,
                        ttl=self.arl_ttl(path),
                    )
                )

        return out

    def do_flush(self, urls: list[str]):
        if not self.env.fastpurge_enabled or not urls:
            LOG.info("fastpurge is not enabled for %s", self.env.name)
            return

        for url in urls:
            LOG.info("fastpurge: flushing", extra=dict(url=url))

        fp = fastpurge.FastPurgeClient(
            auth=dict(
                host=self.env.fastpurge_host,
                access_token=self.env.fastpurge_access_token,
                client_token=self.env.fastpurge_client_token,
                client_secret=self.env.fastpurge_client_secret,
            )
        )

        responses = fp.purge_by_url(urls).result()

        for r in responses:
            LOG.info("fastpurge: response", extra=dict(response=r))

    def run(self):
        urls = self.urls_for_flush
        self.do_flush(urls)

        LOG.info(
            "%s flush of %s URL(s) (%s, ...)",
            "Completed" if self.env.fastpurge_enabled else "Skipped",
            len(urls),
            urls[0] if urls else "<empty>",
        )


def load_task(db: Session, task_id: str):
    return (
        db.query(models.Task)
        .filter(models.Task.id == task_id)
        .with_for_update()
        .first()
    )


@dramatiq.actor(
    time_limit=Settings().actor_time_limit,
    max_backoff=Settings().actor_max_backoff,
)
def flush_cdn_cache(
    paths: list[str],
    env: str,
    settings: Settings = Settings(),
) -> None:
    db = Session(bind=db_engine(settings))
    task_id = CurrentMessage.get_current_message().message_id

    task = load_task(db, task_id)

    if task and task.state == TaskStates.not_started:
        # Mark the task in progress so clients know we're working on it...
        task.state = TaskStates.in_progress
        db.commit()

        # The commit dropped our "for update" lock, so reload it.
        task = load_task(db, task_id)

    if not task or task.state != TaskStates.in_progress:
        LOG.error(
            "Task in unexpected state %s", task.state if task else "<absent>"
        )
        return

    if task.deadline and task.deadline < datetime.utcnow():
        LOG.error("Task exceeded deadline of %s", task.deadline)
        task.state = TaskStates.failed
        db.commit()
        return

    # The CDN config is needed for alias resolution.
    ddb = DynamoDB(
        env=env,
        settings=settings,
        from_date=str(datetime.utcnow()),
        env_obj=get_environment(env, settings),
    )
    definitions = ddb.query_definitions()

    flusher = Flusher(
        paths=paths,
        settings=settings,
        env=env,
        cdn_definitions=definitions,
    )
    flusher.run()

    task.state = TaskStates.complete
    db.commit()
