import logging
import re
from datetime import datetime
from enum import Enum
from os.path import join, normpath
from uuid import UUID

from fastapi import HTTPException, Path
from pydantic import BaseModel, Field, model_validator

from .settings import Settings

LOG = logging.getLogger("exodus-gw")

PathPublishId = Path(
    ...,
    title="publish ID",
    description="UUID of an existing publish object.",
)

PathTaskId = Path(
    ..., title="task ID", description="UUID of an existing task object."
)


def normalize_path(path: str):
    if path:
        path = normpath(path)
        path = "/" + path if not path.startswith("/") else path
    return path


SHA256SUM_PATTERN = re.compile(r"[0-9a-f]{64}")

# TYPE/SUBTYPE[+SUFFIX][;PARAMETER=VALUE]
MIMETYPE_PATTERN = re.compile(r"^[-\w]+/[-.\w]+(\+[-\w]*)?(;[-\w]+=[-\w]+)?")

# Pattern matching anything under /origin/files/sha256 subtree
ORIGIN_FILES_BASE_PATTERN = re.compile("^(/content)?/origin/files/sha256/.*$")

# Pattern which all files under the above base *should* match in order to avoid
# a validation error
ORIGIN_FILES_PATTERN = re.compile(
    "^(/content)?/origin/files/sha256/[0-f]{2}/[0-f]{64}/[^/]{1,300}$"
)


# Note: it would be preferable if we could reuse a settings object loaded by the
# app, however we need this value from within a @classmethod validator.
AUTOINDEX_FILENAME = Settings().autoindex_filename


class ItemPolicyError(HTTPException):
    """Exception type raised when an item provided by the user is
    structurally valid but fails to comply with certain policies.
    """

    def __init__(self, message: str):
        super().__init__(400, detail=message)


class ItemBase(BaseModel):
    web_uri: str = Field(
        ...,
        description="URI, relative to CDN root, which shall be used to expose this object.",
    )
    object_key: str | None = Field(
        "",
        description=(
            "Key of blob to be exposed; should be the SHA256 checksum of a previously uploaded "
            "piece of content, in lowercase hex-digest form. \n\n"
            "Alternatively, the string 'absent' to indicate that no content shall be exposed at the given URI. "
            "Publishing an item with key 'absent' can be used to effectively delete formerly published "
            "content from the point of view of a CDN consumer."
        ),
    )
    content_type: str | None = Field(
        "",
        description="Content type of the content associated with this object.",
    )
    link_to: str | None = Field(
        "", description="Path of file targeted by symlink."
    )

    @model_validator(mode="after")
    def validate_item(self) -> "ItemBase":
        web_uri = self.web_uri
        object_key = self.object_key
        content_type = self.content_type
        link_to = self.link_to
        data = self.__dict__

        if not web_uri:
            raise ValueError("No URI: %s" % data)
        self.web_uri = normalize_path(web_uri)

        if link_to and object_key:
            raise ValueError(
                "Both link target and object key present: %s" % data
            )
        if link_to and content_type:
            raise ValueError("Content type specified for link: %s" % data)

        if link_to:
            self.link_to = normalize_path(link_to)
        elif object_key:
            if object_key == "absent":
                if content_type:
                    raise ValueError(
                        "Cannot set content type when object_key is 'absent': %s"
                        % data
                    )
            elif not re.match(SHA256SUM_PATTERN, object_key):
                raise ValueError(
                    "Invalid object key; must be sha256sum: %s" % data
                )
        else:
            raise ValueError("No object key or link target: %s" % data)

        if content_type:
            # Enforce MIME type structure
            if not re.match(MIMETYPE_PATTERN, content_type):
                raise ValueError("Invalid content type: %s" % data)

        # It's not permitted to explicitly *write* to the autoindex filename,
        # as we don't want anything other than exodus-gw itself to write there.
        # However, clients are allowed to delete (set absent) an index
        # previously generated by exodus-gw.
        if (
            web_uri
            and AUTOINDEX_FILENAME
            and web_uri.split("/")[-1] == AUTOINDEX_FILENAME
            and object_key != "absent"
        ):
            raise ValueError(f"Invalid URI {web_uri}: filename is reserved")

        return self

    def validate_policy(self):
        # Validate additional properties of the item against certain
        # embedded policies.
        #
        # It's a little clumsy that this cannot happen in the @model_validator
        # above. The point is that certain users are allowed to bypass the
        # policy here, whereas the @model_validator is applied too early and
        # too strictly to allow any bypassing.
        self.validate_origin_files()

    def validate_origin_files(self):
        # Enforce correct usage of the /origin/files directory layout.
        if not ORIGIN_FILES_BASE_PATTERN.match(self.web_uri):
            # Not under /origin/files => passes this validation
            return

        # OK, it exists under /origin/files.
        #
        # Paths published under /origin/files must always match the format:
        # /origin/files/sha256/(first two characters of sha256sum)/(full sha256sum)/(basename)
        #
        def policy_error(message: str):
            LOG.warning(message)
            raise ItemPolicyError(message)

        # All content under /origin/files/sha256 must match the regex
        if not re.match(ORIGIN_FILES_PATTERN, self.web_uri):
            policy_error(
                f"Origin path {self.web_uri} does not match regex {ORIGIN_FILES_PATTERN.pattern}"
            )

        # Verify that the two-character partial sha256sum matches the first two characters of the
        # full sha256sum.
        parts = self.web_uri.partition("/files/sha256/")[2].split("/")
        if not parts[1].startswith(parts[0]):
            policy_error(
                f"Origin path {self.web_uri} contains mismatched sha256sum "
                f"({parts[0]}, {parts[1]})"
            )

        # Additionally, every object_key must either be "absent" or equal to the full sha256sum
        # present in the web_uri.
        if self.object_key not in ("absent", parts[1]):
            policy_error(
                f"Invalid object_key {self.object_key} for web_uri {self.web_uri}"
            )


class Item(ItemBase):
    publish_id: UUID = Field(
        ..., description="Unique ID of publish object containing this item."
    )


class FlushItem(BaseModel):
    web_uri: str = Field(
        ...,
        description="URI, relative to CDN root, of which to flush cache",
    )


class PublishStates(str, Enum):
    pending = "PENDING"
    committing = "COMMITTING"
    committed = "COMMITTED"
    failed = "FAILED"

    @classmethod
    def terminal(cls) -> list["PublishStates"]:
        return [cls.committed, cls.failed]


class PublishBase(BaseModel):
    id: str = Field(..., description="Unique ID of publish object.")


class Publish(PublishBase):
    env: str = Field(
        ..., description="""Environment to which this publish belongs."""
    )
    state: PublishStates = Field(
        ..., description="Current state of this publish."
    )
    updated: datetime | None = Field(
        None,
        description="DateTime of last update to this publish. None if never updated.",
    )
    links: dict[str, str] = Field(
        {}, description="""URL links related to this publish."""
    )
    items: list[Item] = Field(
        [],
        description="""All items (pieces of content) included in this publish.""",
    )

    @model_validator(mode="after")
    def make_links(self) -> "Publish":
        _self = join("/", self.env, "publish", str(self.id))
        self.links = {"self": _self, "commit": join(_self, "commit")}
        return self


class TaskStates(str, Enum):
    not_started = "NOT_STARTED"
    in_progress = "IN_PROGRESS"
    complete = "COMPLETE"
    failed = "FAILED"

    @classmethod
    def terminal(cls) -> list["TaskStates"]:
        return [cls.failed, cls.complete]


class Task(BaseModel):
    id: UUID = Field(..., description="Unique ID of task object.")
    publish_id: UUID | None = Field(
        None,
        description="Unique ID of publish object related to this task, if any.",
    )
    state: TaskStates = Field(..., description="Current state of this task.")
    updated: datetime | None = Field(
        None,
        description="DateTime of last update to this task. None if never updated.",
        examples=["2019-08-24T14:15:22Z"],
    )
    deadline: datetime | None = Field(
        None,
        description="DateTime at which this task should be abandoned.",
        examples=["2019-08-24T18:15:22Z"],
    )
    links: dict[str, str] = Field(
        {},
        description="""URL links related to this task.""",
        examples=[{"self": "/task/497f6eca-6276-4993-bfeb-53cbbbba6f08"}],
    )

    @model_validator(mode="after")
    def make_links(self) -> "Task":
        self.links = {"self": join("/task", str(self.id))}
        return self


class AccessResponse(BaseModel):
    url: str = Field(
        description="Base URL of this CDN environment.",
        examples=["https://abc123.cloudfront.net"],
    )
    expires: str = Field(
        description=(
            "Expiration time of access information included in this "
            "response. ISO8601 UTC timestamp."
        ),
        examples=["2024-04-18T05:30Z"],
    )
    cookie: str = Field(
        description="A cookie granting access to this CDN environment.",
        examples=[
            (
                "CloudFront-Key-Pair-Id=K2266GIXCH; "
                "CloudFront-Policy=eyJTdGF0ZW1lbn...; "
                "CloudFront-Signature=kGkxpnrY9h..."
            )
        ],
    )


class MessageResponse(BaseModel):
    detail: str = Field(
        ..., description="A human-readable message with additional info."
    )


class EmptyResponse(BaseModel):
    """An empty object."""


class Alias(BaseModel):
    src: str = Field(
        ..., description="Path being aliased from, relative to CDN root."
    )
    dest: str = Field(
        ..., description="Target of the alias, relative to CDN root."
    )
    exclude_paths: list[str] | None = Field(
        [],
        description="Paths for which alias will not be resolved, "
        "treated as an unanchored regex.",
    )


class YumVariable(Enum):
    releasever = "releasever"
    basearch = "basearch"


class ListingItem(BaseModel):
    var: YumVariable = Field(..., description="YUM variable name.")
    values: list[str] = Field(
        ..., description="Allowed values for YUM variable replacement."
    )


class Config(BaseModel):
    listing: dict[str, ListingItem] = Field(
        ...,
        description=(
            "A mapping from paths to a yum variable name & list of values, "
            "used in generating 'listing' responses."
        ),
    )
    origin_alias: list[Alias] = Field(
        ...,
        description="Aliases relating to /origin.",
    )
    releasever_alias: list[Alias] = Field(
        ...,
        description="Aliases relating to $releasever variables.",
    )
    rhui_alias: list[Alias] = Field(
        ...,
        description="Aliases relating to RHUI.",
    )
