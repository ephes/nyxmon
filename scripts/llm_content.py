import os
import fnmatch

from pathlib import Path


def llm_content():
    """
    Output all relevant code / documentation in the project including
    the relative path and content of each file.
    """

    def echo_filename_and_content(files):
        """Print the relative path and content of each file."""
        for f in files:
            print(f)
            contents = f.read_text()
            relative_path = f.relative_to(project_root)
            print(relative_path)
            print("---")
            print(contents)
            print("---")

    project_root = Path.cwd()
    # Exclude files and directories. This is tuned to make the project fit into the
    # 200k token limit of the claude 3 models.
    exclude_files = {}
    exclude_dirs = {
        ".venv",
        "src/django",
        "src/nyxboard",
        "scripts",
    }
    patterns = ["*.py", "*.rst", "*.js", "*.ts", "*.html"]
    all_files = []
    for root, dirs, files in os.walk(project_root):
        root = Path(root)
        # d is the plain directory name
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for pattern in patterns:
            for filename in fnmatch.filter(files, pattern):
                if filename not in exclude_files:
                    all_files.append(root / filename)
    # print("\n".join([str(f) for f in all_files]))
    echo_filename_and_content(all_files)


if __name__ == "__main__":
    llm_content()
