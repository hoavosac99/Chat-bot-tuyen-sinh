import ctypes
import logging
import os
import shutil
import uuid
from multiprocessing.context import BaseContext  # type: ignore
from pathlib import Path
from urllib import parse
from git import Repo, Reference, PushInfo, RemoteReference, Actor
from git.exc import GitCommandError
from sqlalchemy.orm import Session

import rasax.community.config as rasa_x_config
import rasax.community.constants as constants
from typing import Text, Dict, Union, Optional, List, Iterable, Tuple, TYPE_CHECKING

import rasax.community.initialise  # pytype: disable=import-error
import rasax.community.services.data_service
import rasax.community.utils.common as common_utils
import rasax.community.utils.cli as cli_utils
import rasax.community.utils.io as io_utils
import rasax.community.telemetry as telemetry
from rasax.community.database.admin import GitRepository
from rasax.community.database.service import DbService
import rasax.community.services.integrated_version_control.exceptions
from rasax.community.services.integrated_version_control.ssh_key_provider import (
    GitSSHKeyProvider,
)

if TYPE_CHECKING:
    from multiprocessing import Value, Lock  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_TARGET_BRANCH = "master"

DEFAULT_REMOTE_NAME: Text = "origin"
GIT_BACKGROUND_JOB_ID = "git-synchronization"
SSH_FILES_DIRECTORY = "ssh_files"
DEFAULT_COMMIT_MESSAGE = "Rasa X annotations"
HTTPS_SCHEME = "https"

ASKPASS_NAME = "askpass.sh"
ASKPASS_SCRIPT = """#!/bin/sh
exec echo \"$GIT_PASSWORD\"
"""

SSH_SCRIPT = """#!/bin/sh
ID_RSA={path_to_key}
# Kubernetes tends to reset file permissions on restart. Hence, re-apply the required
# permissions whenever using the key.
chmod 600 $ID_RSA
exec /usr/bin/ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -i $ID_RSA "$@"
"""

PASSWORD_TIMEOUT_S = 60 * 60 * 8  # 8 hours

PUSH_RESULT_BRANCH_KEY = "remote_branch"
PUSH_RESULT_PUSHED_TO_TARGET_BRANCH_KEY = "committed_to_target_branch"
REPOSITORY_SSH_FIELD_KEY = "ssh_key"
REPOSITORY_USE_GENERATED_KEYS_KEY = "use_generated_ssh_keys"
REPOSITORY_URL_KEY = "repository_url"
REPOSITORY_USERNAME_KEY = "username"
REPOSITORY_PASSWORD_KEY = "password"

COMMIT_AUTHOR = Actor("Rasa X", "rasa-x@rasa.com")

# process-safe var. to cache whether the remote repository's target branch is ahead
# and its default value
DEFAULT_IS_TARGET_BRANCH_AHEAD = False

_is_target_branch_ahead = None
# Lock that is used to ensure there's only one Git operation in progress
_git_global_operation_lock = None


def initialize_global_state(mp_context: BaseContext) -> None:
    """Initialize the global state of the module.

    Args:
        mp_context: The current multiprocessing context.
    """

    global _git_global_operation_lock, _is_target_branch_ahead
    _git_global_operation_lock = mp_context.Lock()

    _is_target_branch_ahead = mp_context.Value(
        ctypes.c_bool, DEFAULT_IS_TARGET_BRANCH_AHEAD
    )


def get_git_global_variables() -> Tuple["Lock", "Value"]:
    """Return value of the `_git_global_operation_lock` and `_is_target_branch_ahead` variables.

    Returns:
        A tuple with value of `_git_global_operation_lock` and `_is_target_branch_ahead` variables.
    """
    return (_git_global_operation_lock, _is_target_branch_ahead)


def set_git_global_variables(
    git_global_operation_lock: Optional["Lock"] = None,
    is_target_branch_ahead: Optional["Value"] = _is_target_branch_ahead,
) -> None:
    """Initialize module-level git operation lock."""
    global _git_global_operation_lock
    global _is_target_branch_ahead
    _git_global_operation_lock = git_global_operation_lock
    _is_target_branch_ahead = is_target_branch_ahead


def run_background_synchronization(force_data_injection: bool = False) -> None:
    """Run the git project synchronization.

    This function should be called as a scheduled background process.

    Args:
        force_data_injection: Whether to force injecting data from the local
            repository clone.
    """
    from rasax.community.database import utils as db_utils
    import asyncio  # pytype: disable=pyi-error

    logger.debug("Running scheduled Git synchronization.")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    with db_utils.session_scope() as session:
        git_service = GitService(session, constants.DEFAULT_GIT_REPOSITORY_DIRECTORY)
        loop.run_until_complete(git_service.synchronize_project(force_data_injection))

    loop.close()


class GitService(DbService):
    """This service is used as part of Integrated Version Control to interact with Git.
    """

    def __init__(
        self,
        session: Session,
        directory_for_git_clones: Union[
            Text, Path
        ] = constants.DEFAULT_GIT_REPOSITORY_DIRECTORY,
        project_id: Text = rasa_x_config.project_name,
        repository_id: Optional[int] = None,
    ) -> None:
        """Creates a `GitService` instance for a given repository.

        Args:
            session: The database session.
            directory_for_git_clones: Parent directory which should be used to store
                the cloned repositories.
            project_id: The ID of the project.
            repository_id: The ID of the repository which this service should be
                used with.
        """
        if not _git_global_operation_lock:
            logger.error(
                "Failed to initialize GitService. The lock object has to be "
                "initialized before any attempt to use GitService."
            )
            raise ValueError(
                "Failed to initialize GitService as the concurrency lock "
                "wasn't initialized yet."
            )

        super().__init__(session)
        self._directory_for_git_clones = self._create_directory_for_git_clones(
            directory_for_git_clones
        )

        try:
            self._askpass_path = self._create_askpass_executable(
                directory_for_git_clones
            )
        except ValueError:
            logger.warning(
                f"Could not create GIT_ASKPASS script on '{directory_for_git_clones}'."
                f" Please check that the directory permissions are sufficient for "
                f"reading and writing files."
            )

        self._project: Text = project_id

        self._repository: Optional[Repo] = None
        self._repository_id: Optional[int] = None
        if repository_id:
            self.set_repository(repository_id)

        self._password: Optional[Text] = None

    @staticmethod
    def initialize_global_lock() -> None:
        """Initialize module-level git operation lock.
        This method has to be called first before any attempt to use GitService.
        """
        global _git_global_operation_lock
        _git_global_operation_lock = common_utils.mp_context().Lock()

    @property
    def _is_target_branch_ahead(self) -> bool:
        """Return the value of the process-safe `_is_target_branch_ahead` variable."""
        return _is_target_branch_ahead.value

    @staticmethod
    def _create_directory_for_git_clones(
        directory_for_git_clones: Union[Text, Path]
    ) -> Path:
        try:
            io_utils.create_directory(directory_for_git_clones)
        except OSError as e:
            # This should only happen when running tests locally
            logger.error(e)
            import tempfile

            directory_for_git_clones = tempfile.mkdtemp()

        return Path(directory_for_git_clones)

    @staticmethod
    def _create_askpass_executable(directory_for_git_clones: Union[Text, Path]) -> Path:
        """Ensure the askpass.sh bash/sh script exists and is executable. This
        script is used to provide a password to Git without having to use temp
        files or pipes.

        Args:
            directory_for_git_clones: Path of base directory where all Git
                repositories are cloned into.

        Returns:
            Path to askpass.sh script.
        """
        directory_for_git_clones = Path(directory_for_git_clones)
        askpass_path = (directory_for_git_clones / ASKPASS_NAME).resolve()

        files = io_utils.list_directory(str(directory_for_git_clones))
        if ASKPASS_NAME in files:
            return askpass_path

        io_utils.write_file(askpass_path, ASKPASS_SCRIPT)

        # Make the script executable
        askpass_path.chmod(0o700)

        return askpass_path

    def _target_branch(self) -> Text:
        return self._get_repository(self._repository_id).target_branch

    def get_current_branch(self) -> Text:
        """Get the currently active Git branch.

        Returns:
            Name of the currently active branch.
        """
        return self._repository.active_branch.name

    def create_branch(self, branch_name: Text) -> None:
        """Creates a new branch (does not check it out).

        Args:
            branch_name: Name of the branch.
        """
        self._repository.create_head(branch_name)

    def checkout_branch(
        self, branch_name: Text, force: bool = False, inject_changes: bool = True
    ) -> None:
        """Checks out a branch (`git checkout`).

        Args:
            branch_name: Name of the branch which should be checked out.
            force: If `True` discard any local changes before switching branch.
            inject_changes: If `True` the latest data is injected into the database.
        """
        if not self._fetch():
            return

        try:
            matching_branch: Reference = next(
                branch
                for branch in self._repository.refs
                if branch.name == branch_name
                or branch.name == self._remote_branch_name(branch_name)
            )
        except StopIteration:
            raise ValueError(f"Branch '{branch_name}' does not exist.")

        if isinstance(matching_branch, RemoteReference):
            # Create local branch from remote branch
            matching_branch = self._repository.create_head(
                branch_name, matching_branch
            ).set_tracking_branch(matching_branch)
        # Reset to branch after remote branch was checkout out as local branch
        if force and self.is_dirty():
            self._discard_any_changes()

        matching_branch.checkout()
        logger.debug(f"Checked out branch '{branch_name}'.")

        if inject_changes:
            self.trigger_immediate_project_synchronization()

    def _fetch(self) -> bool:
        """Fetch from the current repository.

        Returns:
            `True` if the fetch operation was successful.
        """
        try:
            self._repository.git.fetch(env=self._env())
            return True
        except GitCommandError:
            repository_info = None
            if self._repository_id is not None:
                repository_info = self._get_repository(self._repository_id)

            if not repository_info or not self._uses_https(repository_info):
                # The repo was cloned via SSH but a problem occurred while
                # fetching, raise the exception again so that it can be handled
                # elsewhere.
                raise

            logger.warning(
                f"Unable to fetch from the remote repository: "
                f"{repository_info.repository_url} It is likely that the cached HTTPS "
                f"credentials have expired or are incorrect."
            )

        return False

    def _discard_any_changes(self) -> None:
        from rasax.community.services import background_dump_service

        logger.debug("Resetting local changes.")

        # Skip dump remaining changes
        background_dump_service.cancel_pending_dumping_job()
        # Remove untracked files
        self._repository.git.clean("-fd")
        # Undo changes to tracked files
        self._reset_to(self._target_branch())
        # Any annotations were removed - we're in a clean state once again
        self._reset_first_annotation()

    def _reset_to(self, branch_name: Text) -> None:
        """Reset the current branch to the state of a specific branch.

        Args:
            branch_name: Branch which the repository should be reset to.
        """
        self._repository.git.reset("--hard", branch_name)

    def is_dirty(self) -> bool:
        """Check if there are uncommitted changes.

        Uses the `first_annotator_id` field to check since data changes might not
        have been dumped to disk yet.

        Returns:
            `True` if there are uncommitted changes, otherwise `False`.
        """
        if not self._repository_id:
            return False

        return self._get_repository().first_annotator_id is not None or self._repository.is_dirty(
            untracked_files=True
        )

    @staticmethod
    def _uses_https(repository: GitRepository) -> bool:
        return parse.urlparse(repository.repository_url).scheme == HTTPS_SCHEME

    def save_repository(
        self, repository_information: Dict[Text, Union[Text, int]]
    ) -> Dict[Text, Union[Text, int]]:
        """Saves the credentials for a repository.

        Args:
            repository_information: The information for the repository as `Dict`.

        Raises:
            ValueError: If the specified credentials were not valid or
                sufficient in order to read and write from the repository.
            RasaLicenseException: If the repository's URL uses HTTPS, but Rasa
                Enterprise is not installed.

        Returns:
             The saved repository information including database ID.
        """
        repository_credentials = self._get_repository_from_dict(repository_information)

        if self._uses_https(repository_credentials):
            if not common_utils.is_enterprise_installed():
                raise common_utils.RasaLicenseException(
                    f"Rasa X does not support cloning repositories over HTTPS. "
                    f"If you'd like to use this feature, please contact us at "
                    f"{constants.HI_RASA_EMAIL} for a Rasa Enterprise license."
                )

            self._password = repository_information.get(REPOSITORY_PASSWORD_KEY)

            try:
                self.assert_valid_https_repository(
                    repository_credentials.repository_url,
                    repository_credentials.username,
                )
            except rasax.community.services.integrated_version_control.exceptions.CredentialsError:
                raise rasax.community.services.integrated_version_control.exceptions.CredentialsError(
                    "Given repository credentials don't provide write "
                    "permissions to the repository. Please make sure the user and "
                    "password are correct and the administrator of the remote "
                    "repository gave you the required permissions."
                )
        else:
            try:
                self.assert_valid_ssh_repository(
                    repository_credentials.repository_url,
                    repository_credentials.ssh_key,
                )
            except rasax.community.services.integrated_version_control.exceptions.CredentialsError:
                raise rasax.community.services.integrated_version_control.exceptions.CredentialsError(
                    "Given repository credentials don't provide write "
                    "permissions to the repository. Please make sure the SSH "
                    "key is correct and the administrator of the remote "
                    "repository gave you the required permissions."
                )

        self.add(repository_credentials)
        self.flush()  # to assign database id

        self._add_ssh_credentials(
            repository_credentials.ssh_key, repository_credentials.id
        )

        io_utils.create_directory(str(self.repository_path(repository_credentials.id)))
        self.set_repository(repository_credentials.id)

        was_created_via_ui = bool(
            repository_information.get(REPOSITORY_USE_GENERATED_KEYS_KEY)
        )
        telemetry.track_repository_creation(
            repository_credentials.target_branch, was_created_via_ui
        )

        return repository_credentials.as_dict()

    def _get_repository_from_dict(
        self,
        repository_information: Dict[Text, Union[Text, int]],
        existing: Optional[GitRepository] = None,
    ) -> GitRepository:
        if existing:
            url = parse.urlparse(existing.repository_url)
        else:
            url = parse.urlparse(repository_information[REPOSITORY_URL_KEY])

        if url.scheme == HTTPS_SCHEME:
            # Check if username was specified
            if not repository_information.get(REPOSITORY_USERNAME_KEY):
                raise rasax.community.services.integrated_version_control.exceptions.GitHTTPSCredentialsError(
                    "Repository information must include username for HTTPS URLs."
                )
        elif repository_information.get(REPOSITORY_USE_GENERATED_KEYS_KEY):
            self._insert_generated_private_ssh_key(repository_information)

        if not existing:
            existing = GitRepository(
                target_branch=DEFAULT_TARGET_BRANCH,
                is_target_branch_protected=False,
                project_id=self._project,
            )

        for key, value in repository_information.items():
            if hasattr(existing, key):
                setattr(existing, key, value)

        return existing

    @staticmethod
    def _insert_generated_private_ssh_key(
        repository_information: Dict[Text, Union[Text, int]]
    ) -> None:
        repository_information[
            REPOSITORY_SSH_FIELD_KEY
        ] = GitSSHKeyProvider.get_private_ssh_key()

    def _add_ssh_credentials(
        self, ssh_key: Text, repository_id: int
    ) -> Dict[Text, Text]:
        base_path = self._directory_for_git_clones / SSH_FILES_DIRECTORY
        io_utils.create_directory(str(base_path))

        path_to_key = base_path / f"{repository_id}.key"
        path_to_executable = self._ssh_executable_path(repository_id)

        return self._add_ssh_credentials_to_disk(
            ssh_key, path_to_key, path_to_executable
        )

    def _add_ssh_credentials_to_disk(
        self, ssh_key: Optional[Text], path_to_key: Path, path_to_executable: Path
    ) -> Dict[Text, Text]:
        env = self._env()

        if not ssh_key:
            return env

        GitService._save_ssh_key(ssh_key, path_to_key)
        GitService._save_ssh_executable(path_to_executable, path_to_key)

        env["GIT_SSH"] = str(path_to_executable)
        return env

    def _env(self) -> Dict[Text, Text]:
        """Return the environment dictionary that should be used when executing
        Git commands.

        Returns:
            Dictionary for use as `env` with `self._repository`.
        """
        env = {"GIT_TERMINAL_PROMPT": "0"}  # Never prompt via stdin

        if self._password:
            env["GIT_ASKPASS"] = str(self._askpass_path)  # Prompt using this script
            env["GIT_PASSWORD"] = self._password

        return env

    @staticmethod
    def _save_ssh_key(ssh_key: Text, path: Path) -> None:
        io_utils.write_file(path, f"{ssh_key}\n")

        # We have to be able to read and write this
        path.chmod(0o600)

    @staticmethod
    def _save_ssh_executable(path: Path, path_to_key: Path) -> None:
        io_utils.write_file(path, SSH_SCRIPT.format(path_to_key=path_to_key))

        # We have to be able read, write and execute the file
        path.chmod(0o700)

    def _ssh_executable_path(self, repository_id: int) -> Path:
        return (
            self._directory_for_git_clones / SSH_FILES_DIRECTORY / f"{repository_id}.sh"
        )

    def get_repositories(self) -> List[Dict[Text, Union[Text, int]]]:
        """Retrieves the stored repository information from the database.

        Returns:
            Stored git credentials (not including ssh keys and access tokens).
        """
        repositories = (
            self.query(GitRepository)
            .filter(GitRepository.project_id == self._project)
            .order_by(GitRepository.id)
            .all()
        )
        serialized_repositories = [credential.as_dict() for credential in repositories]

        return serialized_repositories

    def _get_most_recent_repository(self) -> Optional[GitRepository]:
        """Returns the credentials for the current working directory.

        Returns:
            `None` if no repository information has been stored, otherwise a `Dict` of the
            provided repository details (not including ssh keys and access tokens)..
        """
        return (
            self.session.query(GitRepository).order_by(GitRepository.id.desc()).first()
        )

    def get_repository(self) -> Dict[Text, Union[Text, int]]:
        """Retrieve the repository information for the current repository."""
        return self._get_repository(self._repository_id).as_dict()

    def _get_repository(self, repository_id: Optional[int] = None) -> GitRepository:
        repository_id = repository_id or self._repository_id
        result = (
            self.query(GitRepository).filter(GitRepository.id == repository_id).first()
        )
        if result:
            return result

        raise ValueError(f"Repository with ID '{repository_id}' does not exist.")

    def update_repository(
        self, repository_information: Dict[Text, Union[Text, int]]
    ) -> Dict[Text, Union[Text, int]]:
        """Updates the current repository with new credentials.

        Args:
            repository_information: The values which overwrite the currently
                stored information.

        Raises:
            RasaLicenseException: If the repository's URL uses HTTPS, but Rasa
                Enterprise is not installed.
            ProjectLayoutError: If a new target branch does not contain a valid Rasa
                Open Source project.

        Returns:
            The repository with the updated fields.
        """
        old = self._get_repository(self._repository_id)
        old_target_branch = old.target_branch
        updated = self._get_repository_from_dict(repository_information, old)

        if self._uses_https(updated) and not common_utils.is_enterprise_installed():
            raise common_utils.RasaLicenseException(
                f"Rasa X does not support cloning repositories over HTTPS. "
                f"If you'd like to use this feature, please contact us at "
                f"{constants.HI_RASA_EMAIL} for a Rasa Enterprise license."
            )

        password = repository_information.get(REPOSITORY_PASSWORD_KEY)

        repository_path = self.repository_path(updated.id)
        if password:
            # Set the service's password property (will affect future git commands)
            self._password = password

            if self._is_git_directory(repository_path):
                # Repo is already cloned. Do a fetch so that the new
                # password is cached.
                success = self._fetch()
            else:
                # Repo isn't cloned yet. This can happen if the container
                # was re-created - the background synchronization service
                # won't be able to clone it because it won't have the user
                # password. Doing a clone will cache the password as well.
                success = self.clone()

            if not success:
                raise rasax.community.services.integrated_version_control.exceptions.GitHTTPSCredentialsError(
                    f"The specified password for user '{updated.username}' is "
                    f"incorrect."
                )

        if old_target_branch != updated.target_branch:
            self.checkout_branch(updated.target_branch)
            _assert_valid_project_layout(repository_path)

        return updated.as_dict()

    def delete_repository(self) -> None:
        """Deletes the current repository from the database and the file system."""
        result = self._get_repository(self._repository_id)
        self.delete(result)

        # Generate new keys for the next repository
        GitSSHKeyProvider.generate_new_ssh_keys()

        # Delete cloned directory
        shutil.rmtree(self.repository_path())

    def clone(self) -> bool:
        """Clone the current repository.

        Returns:
            `True` if the repository was cloned successfully.
        """
        repository_info = self._get_repository(self._repository_id)

        try:
            self._clone()
            return True
        except GitCommandError:
            if not self._uses_https(repository_info):
                # The repo was cloned via SSH but a problem occurred while
                # cloning, raise the exception again so that it can be handled
                # elsewhere.
                raise

            logger.warning(
                f"Unable to clone the remote repository: "
                f"{repository_info.repository_url}. It is likely that the cached "
                f"HTTPS credentials have expired or are incorrect."
            )

        return False

    def _clone(self) -> None:
        """Clone the current repository."""

        repository_info = self._get_repository(self._repository_id)
        target_path = self.repository_path(repository_info.id)

        if self._repository or self._is_git_directory(target_path):
            raise ValueError(f"'{target_path}' is already a git directory.")

        repository_url = repository_info.repository_url
        options = []

        logger.info(f"Cloning git repository from URL '{repository_url}'.")

        # Configure authentication
        if self._uses_https(repository_info):
            environment = self._env()

            # Include the username in the remote URL, otherwise git will try to
            # prompt for it.
            url_parts = parse.urlparse(repository_url)
            repository_url = url_parts._replace(
                netloc=f"{repository_info.username}@{url_parts.netloc}"
            ).geturl()

            # Ideally we should specify credential.helper='cache --timeout X'
            # here, but for some reason GitPython splits the arg by spaces,
            # which means that git clone eventually receives "--timeout X",
            # which it does not know how to handle. It's not really important
            # since we then set a timeout in `_configure_credential_cache`. The
            # only important thing is that the cache is used when cloning.
            options.append("--config credential.helper=cache")
        else:
            environment = self._add_ssh_credentials(
                repository_info.ssh_key, repository_info.id
            )

        self._repository_id = repository_info.id
        # We don't have to handle errors here cause we are already doing that when
        # saving the repository.
        # Do a shallow clone of repository which means that we retrieve ony the latest
        # commit of each branch (this is faster than getting the complete history).
        self._repository = Repo.clone_from(
            repository_url,
            target_path,
            env=environment,
            depth=1,
            no_single_branch=True,
            multi_options=options,
        )

        if self._uses_https(repository_info):
            self._configure_credential_cache()

        self.checkout_branch(repository_info.target_branch, inject_changes=False)

        logger.debug(f"Finished cloning git repository '{repository_url}'.")

    def repository_path(self, repository_id: Optional[int] = None) -> Path:
        """Returns the path to the current repository on disk.

        Args:
            repository_id: If a repository id is given, it returns the path to this
                repository instead of the current one.

        Returns:
            Path to repository on disk.
        """
        if not repository_id and self._repository_id:
            repository_id = self._repository_id

        if repository_id:
            return self._directory_for_git_clones / str(repository_id)
        else:
            raise ValueError("No path to Git repository found.")

    def set_repository(self, repository_id: int) -> None:
        """Changes the current repository of the service to another one.

        Args:
            repository_id: ID of the repository which should be used.
        """
        directory = self._directory_for_git_clones / str(repository_id)
        self._repository_id = repository_id

        if self._is_git_directory(directory):
            self._repository = Repo(directory)
            repository = self._get_repository(repository_id)

            if repository.ssh_key:
                self._repository.git.update_environment(
                    GIT_SSH=str(self._ssh_executable_path(self._repository_id))
                )

            if self._uses_https(repository):
                self._configure_credential_cache()
        else:
            self._repository = None

    @staticmethod
    def _is_git_directory(directory: Union[Text, Path]) -> bool:
        return os.path.exists(directory) and ".git" in os.listdir(directory)

    def commit_to(self, branch_name: Text, message: Optional[Text]) -> None:
        """Commit local changes and push them to remote.

        Args:
            branch_name: Branch which the changes should be committed to.
            message: Commit message.

        Returns:
            Information about the commit.
        """
        if branch_name == self._target_branch():
            _ = self._commit_to_target_branch(message)
        else:
            _ = self._commit_to_new_branch(branch_name, message)
            # Change back
            self.checkout_branch(self._target_branch())

    def _commit_to_target_branch(self, message: Optional[Text]) -> Text:
        """Commits changes to the target branch.

        The target branch is e.g. `master` for 'production environment'.

        Args:
            message: The commit message. If `None` a message is generated.

        Returns:
            Name of the target branch.

        Raises:
            GitCommitError: If target branch is protected.
        """
        repository = self._get_repository()
        if repository.is_target_branch_protected:
            raise rasax.community.services.integrated_version_control.exceptions.GitCommitError(
                f"'{repository.target_branch}' is a protected branch. "
                f"You cannot commit and push changes to this branch."
            )

        self._commit_changes(message)
        return repository.target_branch

    def _commit_changes(self, message: Optional[Text]) -> None:
        self._repository.git.add(A=True)
        self._repository.index.commit(message, author=COMMIT_AUTHOR)

    def _commit_to_new_branch(self, branch_name: Text, message: Optional[Text]) -> Text:
        """Commits changes to a new branch.

        Args:
            message: The commit message. If `None` a message is generated.
            branch_name: Branch which should be used.

        Returns:
            Used branch name (the name will be generated).
        """
        self.create_branch(branch_name)
        self.checkout_branch(branch_name, inject_changes=False)
        self._commit_changes(message)

        return branch_name

    def merge_latest_remote_branch(self) -> None:
        """Merges the current branch with its remote branch."""
        current_branch = self.get_current_branch()

        # We only care about file system changes as only these can lead to merge
        # conflicts
        if self.is_dirty():
            logger.debug(
                "Skip inserting the latest remote changes since there are "
                "local changes."
            )
            return

        logger.debug(
            f"Merging remote branch '{current_branch}' into current branch "
            f"'{current_branch}'."
        )
        self._repository.git.merge(self._remote_branch_name(current_branch))

    @staticmethod
    def _remote_branch_name(branch: Text) -> Text:
        return f"{DEFAULT_REMOTE_NAME}/{branch}"

    def _update_and_return_is_remote_branch_ahead(
        self, branch: Optional[Text] = None
    ) -> bool:
        """Determine whether `branch` on remote is ahead of local repository.

        Args:
            branch: Name of the branch to check.
        """
        is_remote_branch_ahead = self._is_remote_branch_ahead(branch)

        # update `_is_target_branch_ahead` if check is against the target branch
        if not branch or branch == self._target_branch():
            _is_target_branch_ahead.value = is_remote_branch_ahead

        return is_remote_branch_ahead

    def _is_remote_branch_ahead(self, other_branch: Optional[Text] = None) -> bool:
        """Checks if `other_branch` has new commits.

        Args:
            other_branch: Remote branch which should be used for comparison. If `None`,
                the remote target branch is used.

        Returns:
            `True` if the remote branch is ahead, `False` if it's not ahead.
        """
        if not other_branch:
            other_branch = self._target_branch()

        current_branch = self.get_current_branch()
        remote_branch = self._remote_branch_name(other_branch)
        commits_behind = self._repository.iter_commits(
            f"{current_branch}..{remote_branch}"
        )
        number_of_commits_behind = sum(1 for _ in commits_behind)

        logger.debug(
            f"Branch '{current_branch}' is {number_of_commits_behind} commits behind "
            f"'{other_branch}'."
        )
        return number_of_commits_behind > 0

    async def commit_and_push_changes_to(
        self, branch_name: Text
    ) -> Dict[Text, Union[Text, bool]]:
        """Commit and push changes to branch.

        Args:
            branch_name: Branch to push changes to.

        Returns:
            Result of the push operation.
        """
        from rasax.community.services import background_dump_service

        await background_dump_service.wait_for_pending_changes_to_be_dumped()

        with common_utils.acquire_lock_no_block(_git_global_operation_lock) as acquired:
            if not acquired:
                logger.debug(
                    f"Failed to acquire a lock for "
                    f"'{self.commit_and_push_changes_to.__name__}' "
                    f"operation. Branch name: {branch_name}. The operation will not be "
                    f"completed."
                )
                raise rasax.community.services.integrated_version_control.exceptions.GitConcurrentOperationException

            self.commit_to(branch_name, DEFAULT_COMMIT_MESSAGE)
            is_committing_to_target_branch = branch_name == self._target_branch()

            result_of_push_operation = self._push(
                branch_name, is_committing_to_target_branch
            )

            telemetry.track_git_changes_pushed(
                result_of_push_operation[PUSH_RESULT_BRANCH_KEY]
            )

            return result_of_push_operation

    def _push(
        self, branch_name: Text, pushing_to_target_branch: bool = True
    ) -> Dict[Text, Union[Text, bool]]:
        """Push committed changes.

        Args:
            branch_name: Branch which should be pushed.
            pushing_to_target_branch: If `True` it will be tried to push to a new branch
                in case pushing to the target branch fails.

        """
        push_results = self._repository.remote().push(branch_name, env=self._env())
        push_succeeded = self._was_push_successful(branch_name, push_results)

        if not push_succeeded and not pushing_to_target_branch:
            raise ValueError(f"Pushing to branch '{branch_name}' failed.")

        if not push_succeeded:
            new_branch_name = f"Rasa-X-change-{uuid.uuid4()}"
            cli_utils.raise_warning(
                f"Pushing to branch '{branch_name}' failed. Please "
                f"ensure that you have write permissions for this branch. Instead, "
                f"committed changes are pushed to a new branch '{new_branch_name}'."
            )

            return self._try_to_push_committed_changes_to_other_branch(new_branch_name)

        if pushing_to_target_branch:
            # A successful push to the target branch means the repository is clean.
            # When pushing to another branch, resetting the first annotator
            # happens in the `_inject_data()` call.
            self._reset_first_annotation()

        return {
            PUSH_RESULT_BRANCH_KEY: branch_name,
            PUSH_RESULT_PUSHED_TO_TARGET_BRANCH_KEY: pushing_to_target_branch,
        }

    @staticmethod
    def _was_push_successful(
        pushed_branch: Text, push_results: Iterable[PushInfo]
    ) -> bool:
        push_result_for_branch = next(
            (
                result
                for result in push_results
                if result.local_ref.name == pushed_branch
            ),
            None,
        )

        if not push_result_for_branch:
            return False
        return push_result_for_branch.flags in [
            PushInfo.UP_TO_DATE,
            PushInfo.NEW_HEAD,
            PushInfo.FAST_FORWARD,
        ]

    def _try_to_push_committed_changes_to_other_branch(
        self, new_branch_name: Text
    ) -> Dict[Text, Union[Text, bool]]:
        """Tries to push committed changes to a new branch and revert the current one.

        Args:
            new_branch_name: Name of the branch to push to.

        Returns:
            The result of the attempt.
        """
        self.create_branch(new_branch_name)
        self.checkout_branch(new_branch_name, inject_changes=False)

        push_result = self._push(new_branch_name, pushing_to_target_branch=False)

        self._revert_to_remote_target_branch()

        return push_result

    def _revert_to_remote_target_branch(self) -> None:
        self.checkout_branch(self._target_branch(), inject_changes=False)
        remote_target_branch = self._remote_branch_name(self._target_branch())
        self._reset_to(remote_target_branch)
        self.trigger_immediate_project_synchronization()

    async def synchronize_project(self, force_data_injection: bool = False) -> None:
        """Synchronizes the Git repository with the database.

        Args:
            force_data_injection: Whether to force injecting data from the local
                repository clone.
        """
        repository_info = self._get_most_recent_repository()
        if not repository_info:
            logger.debug("Skip synchronizing with Git since no credentials were given.")
            return

        # Git directory already exists - let's try to fetch the changes
        self.set_repository(repository_info.id)
        repository_path = self.repository_path(repository_info.id)

        # Set project directory to the directory which is synchronized
        io_utils.set_project_directory(repository_path)

        if not self._is_git_directory(repository_path):
            if self.clone():
                await self._inject_data()

            return

        if not self._fetch():
            # Fetch was not successful due to HTTPS credentials
            return

        # This call has a side effect and has to be called before the force injection
        is_target_branch_ahead = self._update_and_return_is_remote_branch_ahead()

        if force_data_injection:
            return await self._force_inject_latest_remote_changes()

        if not is_target_branch_ahead:
            logger.debug(
                "Remote branch does not contain new changes. No new data "
                "needs to be injected."
            )
            return

        if self.is_dirty():
            logger.debug(
                "Skip synchronizing with Git since the working directory "
                "contains changes. Please commit and push them in order to "
                "pull the latest changes."
            )
            return

        self.merge_latest_remote_branch()
        await self._inject_data()

    async def _inject_data(self) -> None:
        from rasax.community import local

        repository_path = self.repository_path()

        _assert_valid_project_layout(repository_path)

        data_path = repository_path / constants.DEFAULT_RASA_DATA_PATH
        logger.debug("Injecting latest changes from git.")

        await rasax.community.initialise.inject_files_from_disk(
            str(self.repository_path()),
            str(data_path),
            self.session,
            rasa_x_config.default_config_path,
            rasa_x_config.SYSTEM_USER,
        )
        self._reset_first_annotation()

    def _reset_first_annotation(self) -> None:
        if not self._repository_id:
            return

        repository = self._get_repository()

        repository.first_annotator_id = None
        repository.first_annotated_at = None

    async def _force_inject_latest_remote_changes(self) -> None:
        logger.debug("Data injection was forced.")

        if self.is_dirty():
            self._discard_any_changes()

        # If we force an injection, then we want to inject the latest changes
        self.merge_latest_remote_branch()

        await self._inject_data()

        # The next runs should not run with `force_data_injection=True` anymore
        self.remove_force_data_injection_flag_from_project_synchronization()

    def _is_https_authenticated(self, repository: GitRepository) -> bool:
        """Check if the necessary HTTPS credentials are cached for a given
        repository.

        This is a quick way of checking if Git currently has access to the
        HTTPS credentials (password) for doing fetch or push operations. It
        uses the `fill` command for the Git credential manager. See
        https://git-scm.com/docs/git-credential for more information.

        Note: Doing a fetch and checking for errors is actually more reliable,
        since the user could have changed their password since they configured
        IVC. In this case, the password could still be cached, but be invalid,
        and this method would still return `True`. However, doing a fetch is
        much slower than using the Git credential manager.

        Args:
            repository: Git repository information.

        Returns:
            `True` if the password for this repository and username is
            currently cached.
        """
        import tempfile

        url = parse.urlparse(repository.repository_url)

        with tempfile.NamedTemporaryFile(mode="w+") as f:
            f.write(f"username={repository.username}\n")
            f.write(f"host={url.netloc}\n")
            f.write("protocol=https\n")
            f.seek(0)

            try:
                result = self._repository.git.credential(
                    "fill", istream=f, env=self._env()
                )
            except GitCommandError:
                return False

            return "password=" in (result or "")

    def get_repository_status(self) -> Dict[Text, Union[Text, bool, float]]:
        """Returns the current repository status.

        Returns:
            Dictionary containing information (status) about the repository.
        """
        repository = self._get_repository()
        authenticated = True

        if self._uses_https(repository):
            repository_path = self.repository_path(repository.id)

            if self._is_git_directory(repository_path):
                authenticated = self._is_https_authenticated(repository)
            else:
                # The repository is not cloned. This can happen if the
                # container was re-created. It is safe to assume in this case,
                # thay any cached passwords will be lost as well.
                authenticated = False

        dirty = None
        last_pull = None
        branch = None

        if self._repository:
            dirty = self.is_dirty()
            branch = self.get_current_branch()
            last_pull = self._get_latest_fetch_time()

        return {
            "is_committing_to_target_branch_allowed": (
                not self._is_target_branch_ahead
            ),
            "is_remote_ahead": self._is_target_branch_ahead,
            "are_there_local_changes": dirty,
            "current_branch": branch,
            "time_of_last_pull": last_pull,
            "first_annotator_id": repository.first_annotator_id,
            "first_annotated_at": repository.first_annotated_at,
            "authenticated": authenticated,
        }

    def _get_latest_fetch_time(self) -> float:
        fetch_head_path = Path(self._repository.git_dir) / "FETCH_HEAD"
        if fetch_head_path.exists():
            return fetch_head_path.stat().st_mtime
        else:
            logging.debug(
                f"Failed to fetch the latest repository pull time for the repository "
                f"with ID {self._repository_id}. Using the timestamp of the latest "
                f"commit instead."
            )
            return self._repository.commit().committed_datetime.timestamp()

    def assert_valid_https_repository(
        self, repository_url: Text, username: Text
    ) -> None:
        """Test whether authentication credentials give required permissions on
        remote by using HTTPS.

        The test is done by trying to push a branch to the remote repository.

        Args:
            repository_url: URL of the repository to test.
            username: Username to use for authentication.

        Raises:
            ProjectLayoutError: If the connected repository's layout isn't valid.
            CredentialsError: If the user doesn't have the required read / write
                permissions for the repository.
            ValueError: If no repository password has been provided.
        """
        if not self._password:
            raise ValueError("A password is required for HTTPS authentication.")

        self._assert_valid_remote_repository(repository_url, username=username)

    def assert_valid_ssh_repository(
        self, repository_url: Text, ssh_key: Optional[Text]
    ) -> None:
        """Test whether authentication credentials give required permissions on
        remote by using SSH.

        The test is done by trying to push a branch to the remote repository.

        Args:
            repository_url: URL of the repository to test.
            ssh_key: SSH key which should be used for the authentication.

        Raises:
            ProjectLayoutError: If the connected repository's layout isn't valid.
            CredentialsError: If the user doesn't have the required read / write
                permissions for the repository.
        """
        self._assert_valid_remote_repository(repository_url, ssh_key=ssh_key)

    def _configure_credential_cache(self) -> None:
        """Configure the 'cache' Git credential store for the current
        repository."""
        self._repository.git.config(
            ["credential.helper", f"cache --timeout={PASSWORD_TIMEOUT_S}"]
        )

    def _assert_valid_remote_repository(
        self,
        repository_url: Text,
        ssh_key: Optional[Text] = None,
        username: Optional[Text] = None,
    ) -> None:
        """Assert connected repository is eligible to be connected to Rasa X.

        Args:
            repository_url: The SSH or HTTPs connection URL.
            ssh_key: The SSH key if the repository is connected using SSH.
            username: The user name if the repository is connected via HTTPS.

        Raises:
            ProjectLayoutError: If the connected repository's layout isn't valid.
            CredentialsError: If the user doesn't have the required read / write
                permissions for the repository.
        """
        import tempfile

        clone_directory = Path(tempfile.mkdtemp())
        ssh_directory = Path(tempfile.mkdtemp())

        if username:
            git_environment = self._env()

            # Place username inside the repo URL
            url = parse.urlparse(repository_url)
            repository_url = url._replace(netloc=f"{username}@{url.netloc}").geturl()
        else:
            ssh_key_path = ssh_directory / "ssh.key"
            executable_path = ssh_directory / "ssh.sh"
            git_environment = self._add_ssh_credentials_to_disk(
                ssh_key, ssh_key_path, executable_path
            )

        try:
            # Use a shallow clone to speed up the cloning.
            cloned_repository = Repo.clone_from(
                repository_url, clone_directory, env=git_environment, depth=1
            )

            _assert_valid_project_layout(clone_directory)

            # Do a change
            (clone_directory / "test_file").touch()

            # Use a new `GitService` instance to not mess with this one
            git_service = GitService(self.session, clone_directory)
            git_service._repository = cloned_repository
            git_service._password = self._password
            git_service._askpass_path = self._askpass_path
            git_service._configure_credential_cache()

            import uuid

            # Push changes to new branch
            test_branch = f"Rasa-test-branch-{uuid.uuid4()}"
            git_service._commit_to_new_branch(
                test_branch,
                "This is a test in order to check if the user has write permissions "
                "in this repository.",
            )

            git_service._push(test_branch, pushing_to_target_branch=False)

            # Remove pushed changes
            git_service._delete_remote_branch(test_branch)
        except rasax.community.services.integrated_version_control.exceptions.ProjectLayoutError as e:
            cli_utils.raise_warning(str(e))
            raise e
        except Exception as e:
            cli_utils.raise_warning(
                f"An error happened when trying to access '{repository_url}'. It seems "
                f"you don't have to correct permissions for this repository. Please "
                f"check if your credentials are correct and you have write permissions "
                f"in the given repository. The error was: {e}."
            )
            raise rasax.community.services.integrated_version_control.exceptions.CredentialsError()
        finally:
            # Remove temporary directories
            shutil.rmtree(clone_directory)
            shutil.rmtree(ssh_directory)

    def _delete_remote_branch(self, branch_name: Text) -> None:
        """Deletes a remote branch."""
        self._repository.remote().push(f":{branch_name}", env=self._env())

    @staticmethod
    def run_background_synchronization(force_data_injection: bool = False) -> None:
        from rasax.community.database import utils as db_utils

        import asyncio  # pytype: disable=pyi-error

        logger.debug("Running scheduled Git synchronization.")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with db_utils.session_scope() as session:
            git_service = GitService(
                session, constants.DEFAULT_GIT_REPOSITORY_DIRECTORY
            )
            loop.run_until_complete(
                git_service.synchronize_project(force_data_injection)
            )
        loop.close()

    def track_training_data_dumping(
        self, annotator: Text, annotation_time: float
    ) -> None:
        repository = self._get_most_recent_repository()
        if not repository:
            return

        if repository.first_annotator_id:
            return

        repository.first_annotator_id = annotator
        repository.first_annotated_at = annotation_time

    @staticmethod
    def trigger_immediate_project_synchronization() -> None:
        """Trigger an immediate synchronization of the Git repository and the database.
        """
        from rasax.community import scheduler

        scheduler.run_job_immediately(GIT_BACKGROUND_JOB_ID, force_data_injection=True)

    @staticmethod
    def remove_force_data_injection_flag_from_project_synchronization() -> None:
        """Set the `force_data_injection flag to `False` for the next synchronizations.
        """
        from rasax.community import scheduler

        scheduler.modify_job(GIT_BACKGROUND_JOB_ID, force_data_injection=False)


def _assert_valid_project_layout(path: Path) -> None:
    """Assert that the connected repository contains a valid Rasa Open Source project.

    Args:
        path: The path to the directory which should be validated.

    Raises:
        ProjectLayoutError: If project isn't valid.
    """
    if not (path / rasa_x_config.default_domain_path).is_file():
        raise rasax.community.services.integrated_version_control.exceptions.ProjectLayoutError(
            f"The connected repository has an innvalid project layout. "
            f"Integrated Version Control requires the domain to be present at the "
            f"following path: '{rasa_x_config.default_domain_path}'"
        )

    if not (path / rasa_x_config.default_config_path).is_file():
        raise rasax.community.services.integrated_version_control.exceptions.ProjectLayoutError(
            f"The connected repository has an innvalid project layout. "
            f"Integrated Version Control requires the model configuration to be present "
            f"at the following path: '{rasa_x_config.default_domain_path}'"
        )
    if not (path / rasa_x_config.data_dir).is_dir():
        raise rasax.community.services.integrated_version_control.exceptions.ProjectLayoutError(
            f"The connected repository has an innvalid project layout. "
            f"Integrated Version Control requires the training data directory to be "
            f"present at the following path: '{rasa_x_config.default_domain_path}'"
        )
