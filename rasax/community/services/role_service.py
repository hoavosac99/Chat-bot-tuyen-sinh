import logging
from typing import List, Text, Optional, Dict, Any

from ruamel.yaml.compat import ordereddict
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError

import rasax.community.config as rasa_x_config
import rasax.community.constants as constants
from rasax.community.database.admin import Role, Permission, Project, User
from rasax.community.database.service import DbService
from rasax.community.services.user_service import ADMIN, ANNOTATOR, TESTER, UserService

logger = logging.getLogger(__name__)


PERMISSIONS = {
    # This is not a category in the UI, but currently assigned by default.
    # Every user can do this, default permission
    "basic": {
        "view": {
            "user.get",
            "chatToken.get",
            "statistics.get",
            "telemetry.get",
            "entities.list",
        },
        "modify": {
            "user.update",
            "user.password.update",
            "user.values.update",
            "telemetry.delete",
            "telemetry.create",
            "metadata.create",
            "messages.create",
            "clientEvents.create",
        },
    },
    # this cannot be assigned with platform categories currently
    # only admin default role has those permissions
    "advanced": {
        "view": {
            "roles.list",
            "roles.get",
            "roles.users.list",
            "users.list",
            "users.roles.list",
            "allEvaluations.list",
            "actions.get",
            "domainWarnings.get",
            "config.get",
        },
        "modify": {
            "roles.create",
            "roles.update",
            "roles.delete",
            "roles.users.update",
            "users.roles.update",
            "users.roles.delete",
            "features.update",
            "users.delete",
            "users.create",
            "projects.create",
            "clientEvaluation.update",
            "clientEvaluation.delete",
            "allEvaluations.create",
            "chatToken.update",
            "actions.create",
            "actions.update",
            "actions.delete",
        },
    },
    "test_conversation": {
        "view": {"clients.get", "clientMessages.list", "actionPrediction.get"}
    },
    "conversations": {
        "view": {
            "metadata.get",
            "metadata.list",
            "logs.list",
            "clientEvaluation.get",
            "conversationActions.list",
            "conversationEntities.list",
            "conversationIntents.list",
            "conversationPolicies.list",
            "conversationTags.list",
            "conversationSlotNames.list",
            "conversationSlotValues.list",
            "conversationInputChannels.list",
        },
        "modify": {
            "messageFlags.update",
            "messageFlags.delete",
            "metadata.delete",
            "actionPrediction.create",
            "conversationTags.create",
            "conversationTags.update",
            "conversationTags.delete",
            "conversationReviewStatus.update",
        },
    },
    "nlu_training_data": {
        "view": {
            "examples.list",
            "examples.get",
            "regexes.list",
            "regexes.get",
            "bulkData.get",
            "logs.list",
            "logs.get",
            "warnings.get",
            "intents.list",
            "lookup_tables.list",
            "lookup_tables.get",
            "entity_synonyms.list",
            "entity_synonyms.get",
        },
        "modify": {
            "examples.update",
            "examples.delete",
            "logs.delete",
            "logs.create",
            "examples.create",
            "bulkData.update",
            "messageIntents.update",
            "messageIntents.delete",
            "intents.create",
            "intents.update",
            "intents.delete",
            "userGoals.create",
            "userGoals.delete",
            "regexes.create",
            "regexes.update",
            "regexes.delete",
            "lookup_tables.create",
            "lookup_tables.delete",
            "entity_synonyms.create",
            "entity_synonyms.update",
            "entity_synonyms.delete",
            "entity_synonym_values.create",
            "entity_synonym_values.delete",
            "entity_synonym_values.update",
        },
    },
    "stories": {
        "view": {"stories.list", "stories.get", "bulkStories.get"},
        "modify": {
            "stories.create",
            "bulkStories.update",
            "stories.update",
            "stories.delete",
            "clientEvents.update",
        },
    },
    "rules": {
        "view": {"rules.list"},
        "modify": {"rules.create", "bulkRules.update", "rules.update", "rules.delete",},
    },
    "tests": {"modify": {"tests.create"}},
    "responses": {
        "view": {"responseTemplates.list"},
        "modify": {
            "bulkResponseTemplates.update",
            "responseTemplates.create",
            "responseTemplates.delete",
            "responseTemplates.update",
            "nlgResponse.create",
        },
    },
    "models": {
        "view": {
            "models.list",
            "models.settings.get",
            "models.get",
            "models.modelByTag.get",
            "models.evaluations.list",
            "models.evaluations.get",
        },
        "modify": {
            "models.delete",
            "models.tags.update",
            "models.tags.delete",
            "models.jobs.create",
            "models.settings.update",
            "models.evaluations.delete",
            "models.evaluations.update",
            "models.create",
        },
    },
    "analytics_dashboard": {"view": {"analytics.get"}},
    "deployment_environment": {
        "view": {"environments.list"},
        "modify": {"environments.update"},
    },
    "git": {
        "view": {
            "repositories.list",
            "repositories.get",
            "repository_status.get",
            "public_ssh_key.get",
        },
        "modify": {
            "repositories.create",
            "repositories.update",
            "repositories.delete",
            "branch.update",
            "commit.create",
        },
    },
    "domain": {"view": {"domain.get", "domain.list"}, "modify": {"domain.update"}},
}

DEFAULT_ROLES = {
    ADMIN: [
        "basic.*",
        "advanced.*",
        "test_conversation.*",
        "conversations.*",
        "nlu_training_data.*",
        "stories.*",
        "rules.*",
        "tests.*",
        "responses.*",
        "models.*",
        "analytics_dashboard.*",
        "deployment_environment.*",
        "git.*",
        "domain.*",
    ],
    ANNOTATOR: [
        "basic.*",
        "test_conversation.*",
        "nlu_training_data.*",
        "models.view.*",
        "responses.*",
        "stories.*",
        "rules.*",
        "tests.*",
        "conversations.view.metadata.get",
    ],
    TESTER: ["basic.*", "test_conversation.*", "models.view.*"],
}


def _strip_category_from_permissions(permissions: List[Text]) -> List[Text]:
    return [".".join(p.split(".")[2:]) for p in permissions]


def guest_permissions() -> List[Text]:
    """Returns list of guest user permissions.

    These are defined by the `basic` and `test_conversation` UI categories.
    """

    basic_permissions = permissions_for_category("basic")
    test_conversation_permissions = permissions_for_category("test_conversation")

    return _strip_category_from_permissions(
        basic_permissions + test_conversation_permissions
    )


def _category_for_annotation(annotation: Text) -> Optional[Text]:
    """Return base category for `annotation`.

    Example: annotation 'user.get' returns 'basic.view'
    """

    for category, mode_dict in PERMISSIONS.items():
        for mode, permissions in mode_dict.items():
            for permission in permissions:
                if annotation == permission:
                    return ".".join((category, mode))

    return None


def permission_from_route_annotation(annotation: Text) -> Optional[Text]:
    """Return full permission from route annotation.

    Example: annotation 'user.get' returns 'basic.view.user.get'
    """

    category = _category_for_annotation(annotation)
    if category:
        return ".".join((category, annotation))

    raise ValueError(f"Could not find category for annotation '{annotation}'.")


def permissions_for_category(category: Text) -> List[Text]:
    """Return all permissions associated with base category.

    Example: category 'basic' returns

        ['basic.view.user.get',
        'basic.view.chatToken.get',
        'basic.view.statistics.get',
        'basic.modify.user.update',
        'basic.modify.user.password.update']
    """

    if category not in PERMISSIONS:
        raise ValueError(f"Could not find category '{category}' in permissions.")

    return [
        ".".join((category, k, p)) for k, v in PERMISSIONS[category].items() for p in v
    ]


def normalise_permissions(perms: List[Text]) -> List[Text]:
    """Normalises permission strings to be sanic-jwt-compatible.

    Platform permissions are of the form `a.b.c.action`, but sanic-jwt expects
    `a.b.c:action`.
    """

    out = []
    for p in perms:
        k = p.rfind(".")
        out.append(p[:k] + ":" + p[k + 1 :])

    return out


class RoleService(DbService):
    @property
    def default_roles(self) -> Dict[Text, Any]:
        """Dictionary of default Rasa X roles.

        Preserves key order when dumped to yaml.
        """

        return ordereddict(DEFAULT_ROLES)

    @property
    def api_permissions(self) -> List[Text]:
        """Retrieve list of all existing API permissions."""

        permissions_list = []
        for category, actions in PERMISSIONS.items():
            for mode, action_set in actions.items():
                for action in action_set:
                    permissions_list.append(".".join((category, mode, action)))

        return permissions_list

    @property
    def roles(self) -> List[Text]:
        """Retrieve list of all existing API roles."""

        return [r for (r,) in self.query(Role.role).distinct().all()]

    def get_role(self, role: Text) -> Role:
        """Fetch role."""

        return self.query(Role).filter(Role.role == role).first()

    def get_default_role(self) -> Optional[Text]:
        """Fetch default role name."""

        default_role = self._get_default_role()
        if default_role:
            return default_role.role

        return None

    def _get_default_role(self) -> Role:
        return self.query(Role).filter(Role.is_default).first()

    def set_default_role(self, role: Role) -> None:
        """Set default role.

        Sets `is_default` to False for the existing default role.
        """

        old_default_role = self._get_default_role()
        if old_default_role:
            old_default_role.is_default = False

        role.is_default = True

    def init_roles(
        self, overwrite: bool = False, project_id: Text = rasa_x_config.project_name
    ):
        """Initialize the role system."""

        if not overwrite and self.roles:
            # if there are already roles present,
            # we wont overwrite until forced
            return

        # save roles
        for role in [*DEFAULT_ROLES]:
            # default role is defined as environment variable
            is_default = role == rasa_x_config.saml_default_role
            self.save_role(role, is_default=is_default)

        # save permissions
        for role, permissions in self.default_roles.items():
            self.save_permissions_for_role(role, permissions, project_id)

    def save_role(
        self,
        role: Text,
        description: Optional[Text] = None,
        is_default: Optional[bool] = None,
    ) -> None:
        """Save API role."""

        existing = self.query(Role).filter(Role.role == role).first()

        if existing:
            return existing.as_dict()

        user_role = Role(role=role, description=description)
        if is_default:
            self.set_default_role(user_role)

        self.add(user_role)

    def delete_role(self, role: Text) -> None:
        """Delete API role."""

        if role == ADMIN:
            raise ValueError("Admin role cannot be deleted.")

        role = self.query(Role).filter(Role.role == role).first()

        self.delete(role)

    def get_stripped_role_permissions(self, roles: List[Role]) -> List[Text]:
        """Returns a list of stripped permissions for `roles`.

        A stripped permissions does not include the `category.mode` prefix, e.g.
        `models.list` instead of `models.view.models.list.
        """

        permissions = []
        for role in roles:
            role_permissions = [p.permission for p in role.permissions]
            permissions += self.expand_wildcard_permissions(role_permissions)

        return _strip_category_from_permissions(permissions)

    def _expand_wildcard_permission(self, permission: Text) -> List[Text]:
        permissions_list = []
        if "*" in permission:
            permission = permission.replace("*", "")
            for p in self.api_permissions:
                if p.startswith(permission):
                    permissions_list.append(p)

        elif permission in self.api_permissions:
            permissions_list.append(permission)

        return permissions_list

    def expand_wildcard_permissions(
        self, unexpanded_permissions: List[Text]
    ) -> List[Text]:
        """Expand API permissions with wildcard syntax (`*`)."""

        permission_list = set()
        for expanded in map(self._expand_wildcard_permission, unexpanded_permissions):
            permission_list.update(expanded)

        return list(permission_list)

    def get_role_permissions(
        self, role: Text, project_id: Text = rasa_x_config.project_name
    ) -> List[Text]:
        """Retrieve list of API permissions associated with `role`."""

        results = (
            self.query(Permission.permission)
            .filter(and_(Permission.role_id == role, Permission.project == project_id))
            .all()
        )
        results = [p for (p,) in results]

        return self.expand_wildcard_permissions(results)

    def get_user_permissions(
        self,
        username: Text,
        user_service: UserService,
        project_id: Text = rasa_x_config.project_name,
    ) -> List[Text]:
        """Retrieve list of API permissions associated with `username`."""

        user = user_service.fetch_user(username)
        return self._permissions_for(user, project_id)

    def _permissions_for(
        self, user: Dict, project_id: Text = rasa_x_config.project_name
    ) -> List[Text]:
        user_roles = user.get("roles", [])
        user_permissions = set()
        for role in user_roles:
            user_permissions.update(self.get_role_permissions(role, project_id))

        return list(user_permissions)

    def save_permissions_for_role(
        self,
        role: Text,
        permissions_list: List[Text],
        project_id: Text = rasa_x_config.project_name,
    ) -> Dict[Text, Any]:
        """Save API permissions for `role`."""

        (project_id,) = (
            self.query(Project.project_id)
            .filter(Project.project_id == project_id)
            .first()
        )

        if project_id:
            # permissions may contain duplicate entries which have to be removed
            permissions = list(set(permissions_list))
            permission_objects = [
                Permission(role_id=role, permission=p, project=project_id)
                for p in permissions
            ]

            try:
                self.bulk_save_objects(permission_objects)
            except IntegrityError as e:
                logger.debug(
                    "Permissions '{}' could not be saved due to '{}'."
                    "".format(permission_objects, e)
                )
                self.rollback()

        return {
            "role": role,
            "permissions": self.get_role_permissions(role, project_id),
        }

    def delete_permissions_for_role(
        self, role: Text, project_id: Text = rasa_x_config.project_name
    ) -> None:
        """Delete API permissions for `role`."""

        permissions = (
            self.query(Permission)
            .filter(and_(Permission.role_id == role, Permission.project == project_id))
            .all()
        )
        self.delete_all(permissions)

    def backend_to_frontend_format_roles(
        self, backend_roles: List[Text]
    ) -> List[Dict[Text, Any]]:
        """Translate roles from backend format into the one expected
       by the frontend.

       input format: List[category.mode.specifics] (for example: basic.view.roles.get)
       output format: List[{"role": "role_name",
                            "grants": {"categoryA":['read:any'],
                                    "categoryB": ...}}]
       """

        crud_any = ["create:any", "update:any", "delete:any", "read:any"]

        ui_categories = [*PERMISSIONS]
        roles = []
        for role in backend_roles:
            granted = {}
            permissions_list = self.get_role_permissions(role)

            # check for each permission category if granted
            # if "modify" permission is given, "view" is added automatically
            for permission in permissions_list:
                for item in ui_categories:
                    if permission.startswith(item + ".modify"):
                        granted[item] = crud_any
                    elif permission.startswith(item) and item not in granted:
                        granted[item] = ["read:any"]

            _role = self.get_role(role)
            roles.append(
                {
                    "role": role,
                    "grants": granted,
                    "is_default": _role.is_default,
                    "description": _role.description,
                    "users_count": self.count_role_users(
                        role, exclude_system_user=True
                    ),
                }
            )

        return roles

    def frontend_to_backend_format_permissions(
        self, frontend_permissions: Dict[Text, List]
    ) -> List[Text]:
        """Translate permissions from frontend format into the one expected
        by the backend.

        input format: ordereddict([('categoryA', ['read:any']), ...])
        output format: List[category.mode.specifics] (for example: basic.view.roles.get)
        """

        granted = []
        for category, permissions in frontend_permissions.items():
            if "create:any" in permissions:  # "modify" permission
                granted.append(category + ".*")
            elif permissions:  # other permissions are of type "view"
                granted.append(category + ".view.*")

            granted = self.expand_wildcard_permissions(granted)

        return granted

    def fetch_role_users(
        self, role: Text, exclude_system_user: bool = True
    ) -> List[Dict[Text, Any]]:
        """Retrieve list of all existing usernames for role

        Excludes the system user from the query if `exclude_system_user` is True.
        """

        query = User.roles.any(Role.role == role)

        if exclude_system_user:
            query = and_(query, User.username != rasa_x_config.SYSTEM_USER)

        users = self.query(User).filter(query).all()

        return [user.as_dict() for user in users]

    def count_role_users(self, role: Text, exclude_system_user: bool = False) -> int:
        """Retrieve the number of all existing users for a specified role.

        Args:
            role: Name of the role.
            exclude_system_user: A boolean that indicates if the `system_user`
                needs to be excluded from the count query.

        Returns:
            Number of users.
        """

        query = User.roles.any(Role.role == role)

        if exclude_system_user:
            query = and_(query, User.username != rasa_x_config.SYSTEM_USER)

        return self.query(User).filter(query).count()

    def update_role(
        self,
        role: Text,
        name: Text,
        frontend_permissions: Optional[Dict[Text, List]],
        user_service: UserService,
        description: Optional[Text] = None,
        is_default: Optional[bool] = None,
    ) -> None:
        """Update role with new name and/ or new permissions."""

        if name != role:
            self._update_role_name(name, role, user_service)

        elif role not in self.roles:
            self.save_role(role, description, is_default)

        elif frontend_permissions:  # delete existing permissions if a new set is passed
            self.delete_permissions_for_role(role)

        if is_default is not None:
            self._update_role_default_status(role, is_default)

        if description is not None:
            self._update_role_description(role, description)

        if frontend_permissions:
            permissions = self.frontend_to_backend_format_permissions(
                frontend_permissions
            )
            permissions.extend(self.expand_wildcard_permissions(["basic.*"]))
            self.save_permissions_for_role(name, permissions)

    def update_role_users(
        self, role: Text, users: List[Text], user_service: UserService
    ) -> List[Dict[Text, Any]]:
        """Update all users associated with this role.

        Args:
            role: Name of the role.
            users: List of all usernames of users that need to be assigned to a specified `role`.
            user_service: An instance of the `UserService`.

        Returns:
            List of all existing usernames for this role.
        """

        # delete this role from all users
        for user in self.fetch_role_users(role, exclude_system_user=True):
            username = user[constants.USERNAME_KEY]
            user_service.delete_user_role(username, role)

        # add role to all users_to_update
        for username in users:
            existing_user = user_service.fetch_user(username)
            if existing_user:
                user_service.add_role_to_user(username, role)

        return self.fetch_role_users(role)

    def _update_role_name(
        self, new_name: Text, old_name: Text, user_service: UserService
    ) -> None:
        """Update the name of a role and move its users."""

        # a rename should preserve old description and its `is_default` status
        existing_role = self.get_role(old_name)
        self.save_role(new_name, existing_role.description, existing_role.is_default)

        # move users with old role name to new role name
        users = self.fetch_role_users(old_name, exclude_system_user=True)
        for user in users:
            user_service.replace_user_roles(user[constants.USERNAME_KEY], [new_name])
        if old_name in self.roles:
            self.delete_role(old_name)

    def _update_role_description(self, role: Text, description: Text) -> None:
        """Update the description of a role."""

        _role = self.get_role(role)
        if _role:
            _role.description = description

    def _update_role_default_status(self, role: Text, is_default: bool) -> None:
        """Update the default status of a role."""

        _role = self.get_role(role)
        if _role:
            if is_default:
                self.set_default_role(_role)
            else:
                _role.is_default = False

    def create_role(
        self,
        role: Text,
        frontend_permissions: Dict[Text, List],
        description: Optional[Text] = None,
        is_default: Optional[bool] = None,
    ) -> None:
        """Create a new role with its permissions."""

        backend_permissions = self.frontend_to_backend_format_permissions(
            frontend_permissions
        )

        # all roles receive basic permissions
        backend_permissions.extend(self.expand_wildcard_permissions(["basic.*"]))

        self.save_role(role, description, is_default)
        self.save_permissions_for_role(role, backend_permissions)

    def is_user_allowed_to_view_all_conversations(
        self, user: Optional[Dict[Text, Any]]
    ) -> bool:
        """Checks if user is allowed to view trackers of all conversations."""
        if not user:
            return False

        user_permissions = self._permissions_for(user)
        return "conversations.view.metadata.list" in user_permissions
