import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import worktrees


def git(path, *args):
    return subprocess.run(["git", "-C", path, *args], check=True, capture_output=True, text=True).stdout.strip()


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        self.origin, self.repo, self.tree = (os.path.join(root, name) for name in ("origin.git", "repo", "tree"))
        subprocess.run(["git", "init", "--bare", "-b", "main", self.origin], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", self.repo], check=True, capture_output=True)
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        self.commit(self.repo, "base")
        git(self.repo, "remote", "add", "origin", self.origin)
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "worktree", "add", "-q", "-b", "feature", self.tree)

    def commit(self, path, name):
        with open(os.path.join(path, name), "w") as f:
            f.write(name)
        git(path, "add", name)
        git(path, "commit", "-q", "-m", name)

    def info(self):
        return worktrees.linked_worktree(self.tree)


class LinkedWorktreeTest(WorktreeTest):
    def test_main_checkout_is_not_a_linked_worktree(self):
        self.assertIsNone(worktrees.linked_worktree(self.repo))

    def test_directory_outside_git_is_not_a_linked_worktree(self):
        self.assertIsNone(worktrees.linked_worktree(os.path.dirname(self.repo)))

    def test_missing_directory_is_not_a_linked_worktree(self):
        self.assertIsNone(worktrees.linked_worktree(os.path.join(self.repo, "gone")))

    def test_worktree_is_described_by_its_path_and_branch(self):
        info = self.info()
        self.assertEqual((info["path"], info["branch"], info["head"]), (self.tree, "feature", git(self.tree, "rev-parse", "HEAD")))

    def test_subdirectory_resolves_to_the_worktree_root(self):
        os.mkdir(os.path.join(self.tree, "sub"))
        self.assertEqual(worktrees.linked_worktree(os.path.join(self.tree, "sub"))["path"], self.tree)


class BlockerTest(WorktreeTest):
    def test_clean_worktree_whose_commits_are_pushed_has_no_blocker(self):
        self.assertIsNone(worktrees.blocker(self.info(), []))

    def test_untracked_file_blocks(self):
        open(os.path.join(self.tree, "new"), "w").close()
        self.assertEqual(worktrees.blocker(self.info(), []), "it has uncommitted changes")

    def test_modified_tracked_file_blocks(self):
        with open(os.path.join(self.tree, "base"), "w") as f:
            f.write("changed")
        self.assertEqual(worktrees.blocker(self.info(), []), "it has uncommitted changes")

    def test_commit_that_is_not_pushed_blocks(self):
        self.commit(self.tree, "work")
        self.assertEqual(worktrees.blocker(self.info(), []), "it has commits that are not pushed")

    def test_pushed_commit_does_not_block(self):
        self.commit(self.tree, "work")
        git(self.tree, "push", "-q", "origin", "feature")
        self.assertIsNone(worktrees.blocker(self.info(), []))

    def test_another_pane_in_the_worktree_blocks(self):
        self.assertEqual(worktrees.blocker(self.info(), [self.tree + "/sub"]), "another pane is open in it")

    def test_pane_elsewhere_does_not_block(self):
        self.assertIsNone(worktrees.blocker(self.info(), [self.repo, self.tree + "-other"]))


class RemoveTest(WorktreeTest):
    def test_directory_is_removed(self):
        worktrees.remove(self.info())
        self.assertFalse(os.path.exists(self.tree))

    def test_merged_branch_is_deleted(self):
        worktrees.remove(self.info())
        self.assertEqual(git(self.repo, "branch", "--list", "feature"), "")

    def test_branch_with_unmerged_commits_is_kept(self):
        self.commit(self.tree, "work")
        worktrees.remove(self.info())
        self.assertIn("feature", git(self.repo, "branch", "--list", "feature"))

    def test_failure_raises_with_what_git_said(self):
        info = {**self.info(), "path": self.tree + "-missing"}
        with self.assertRaisesRegex(RuntimeError, "not a working tree"):
            worktrees.remove(info)


class RestoreTest(WorktreeTest):
    def test_kept_branch_is_checked_out_again(self):
        self.commit(self.tree, "work")
        info = self.info()
        worktrees.remove(info)
        worktrees.restore(info)
        self.assertEqual(git(self.tree, "branch", "--show-current"), "feature")

    def test_deleted_branch_is_recreated_at_its_old_commit(self):
        info = self.info()
        worktrees.remove(info)
        worktrees.restore(info)
        self.assertEqual((git(self.tree, "branch", "--show-current"), git(self.tree, "rev-parse", "HEAD")), ("feature", info["head"]))

    def test_detached_worktree_is_restored_detached(self):
        git(self.tree, "checkout", "-q", "--detach")
        info = self.info()
        worktrees.remove(info)
        worktrees.restore(info)
        self.assertEqual((git(self.tree, "branch", "--show-current"), git(self.tree, "rev-parse", "HEAD")), ("", info["head"]))


if __name__ == "__main__":
    unittest.main()
