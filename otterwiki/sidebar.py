#!/usr/bin/env python
# vim: set et ts=8 sts=4 sw=4 ai:

import os
import re
import json
from collections import OrderedDict
from timeit import default_timer as timer
from flask import url_for
from otterwiki.server import storage, app
from otterwiki.util import (
    split_path,
    join_path,
    empty,
)
from otterwiki.helper import (
    get_pagename,
)
from otterwiki.page_titles import get_page_title_manager


class SidebarMenu:
    URI_SIMPLE = re.compile(r"^(((https?)\:\/\/)|(mailto:))\S+")

    def __init__(self):
        self.menu = []
        self.config = []
        if not app.config.get("SIDEBAR_CUSTOM_MENU", None):
            return
        try:
            raw_config = json.loads(
                app.config.get("SIDEBAR_CUSTOM_MENU", "[]")
            )
        except (ValueError, IndexError) as e:
            app.logger.error(
                f"Error decoding SIDEBAR_CUSTOM_MENU={app.config.get('SIDEBAR_CUSTOM_MENU','')}: {e}"
            )
            raw_config = []
        # generate both config and menu from raw_config
        for entry in raw_config:
            if not entry.get("title", None) and not entry.get("link", None):
                continue
            link, title = entry.get("link", ""), entry.get("title", "")
            self.config.append({"link": link, "title": title})
            if empty(link):
                if empty(title):
                    continue
                self.menu.append(
                    {"link": url_for("view", path=title), "title": title}
                )
            elif self.URI_SIMPLE.match(link):
                if empty(title):
                    title = link
                self.menu.append({"link": link, "title": title})
            else:
                if empty(title):
                    title = link
                self.menu.append(
                    {"link": url_for("view", path=link), "title": title}
                )

    def query(self):
        return self.menu


class SidebarPageIndex:
    AXT_HEADING = re.compile(
        r' {0,3}(#{1,6})(?!#+)(?: *\n+|' r'\s+([^\n]*?)(?:\n+|\s+?#+\s*\n+))'
    )
    SETEX_HEADING = re.compile(r'([^\n]+)\n *(=|-){2,}[ \t]*\n+')

    def __init__(self, path: str = "/", mode: str = ""):
        self.path = (
            path if app.config["RETAIN_PAGE_NAME_CASE"] else path.lower()
        )
        self.path_depth = len(split_path(self.path))
        try:
            self.max_depth = int(app.config["SIDEBAR_MENUTREE_MAXDEPTH"])
        except ValueError:
            self.max_depth = None
        self.mode = app.config["SIDEBAR_MENUTREE_MODE"]
        self.focus = app.config["SIDEBAR_MENUTREE_FOCUS"]
        # overwrite mode if argument is given
        if mode:
            self.mode = mode

        self.filenames_and_header = []

        # load pages
        if self.mode == "":
            self.tree = None
        else:
            self.tree = OrderedDict()
            # load all siblings and parents of the current page
            self.load(self.path)
            # check if focus has been disabled, via SIDEBAR_MENUTREE_FOCUS
            if self.focus == "OFF" and path != "":
                # without focus load all pages
                self.load(path="")
            self.tree = self.order_tree(self.tree)

    def read_header(self, filename):
        filehead = storage.load(filename, size=512)
        # find first markdown header in filehead
        header = [line for (_, line) in self.AXT_HEADING.findall(filehead)]
        header += [line for (line, _) in self.SETEX_HEADING.findall(filehead)]
        if len(header):
            return header[0]
        return None

    def order_tree(
        self,
        tree: OrderedDict,
    ):
        # convert OrderedDict into list
        entries = list(tree.items())
        # decide sort_key lambda on mode
        if app.config["SIDEBAR_MENUTREE_IGNORE_CASE"]:
            sort_key = lambda k: (True, str.lower(k[0]))
        else:
            sort_key = lambda k: (True, k[0])
        if self.mode in ["DIRECTORIES_GROUPED"]:
            if app.config["SIDEBAR_MENUTREE_IGNORE_CASE"]:
                sort_key = lambda k: (
                    len(k[1]["children"]) == 0,
                    str.lower(k[0]),
                )
            else:
                sort_key = lambda k: (len(k[1]["children"]) == 0, k[0])
        # sort entries
        filtered_list = sorted(entries, key=sort_key)
        # filter entries
        if self.mode in ["DIRECTORIES_ONLY"]:
            filtered_list = [
                x for x in filtered_list if len(x[1]["children"]) > 0
            ]
        # after filtering and ordering: back to OrderedDict
        stree = OrderedDict(filtered_list)
        # recursively take care of the child nodes
        for key, values in stree.items():
            if values["children"]:
                stree[key]["children"] = self.order_tree(
                    values["children"],
                )
        return stree

    def add_node(self, tree, prefix, parts, header=None):
        # handle max_depth
        if (
            self.max_depth
            and len(prefix) + len(parts) > self.path_depth + self.max_depth
        ):
            return
        if parts[0] not in tree:
            # Get page title manager
            title_manager = get_page_title_manager()
            pagepath = join_path(prefix + parts)

            if title_manager and title_manager.enabled:
                # Use page title manager for display names, but keep original path for URLs
                display_title_short = title_manager.get_display_title(
                    pagepath, full=False
                )
                # Use original filename-based path for navigation when title manager is enabled
                url_path = get_pagename(
                    pagepath,
                    full=True,
                    header=header if len(parts) == 1 else None,
                )
            else:
                # Fall back to original behavior - both display and URL use same logic
                url_path = get_pagename(
                    pagepath,
                    full=True,
                    header=header if len(parts) == 1 else None,
                )
                display_title_short = get_pagename(
                    pagepath,
                    full=False,
                    header=header if len(parts) == 1 else None,
                )

            tree[parts[0]] = {
                "children": OrderedDict(),
                "path": url_path,  # This is used for URL generation
                "header": display_title_short,  # This is used for display
            }
        if len(parts) > 1:
            self.add_node(
                tree[parts[0]]["children"],
                prefix + [parts[0]],
                parts[1:],
                header,
            )

    def load(self, path):
        t_start = timer()
        files, _ = storage.list(p=path)
        app.logger.debug(
            f"SidebarPageIndex.load({path}) storage.list() files took {timer() - t_start:.3f} seconds."
        )

        t_start = timer()
        entries = []
        for filename in [
            f for f in files if f.endswith(".md")
        ]:  # filter .md files
            filename = os.path.join(path, filename)
            parents = split_path(filename)[:-1]
            # ensure all parents are in the entries
            for i in range(len(parents)):
                pp = join_path(parents[0 : i + 1])
                entries.append(pp)
            entries.append(filename)
        entries = sorted(list(set(entries)))

        for entry in entries:
            header = None
            if entry.endswith(".md"):
                header = self.read_header(entry)
                entry = entry[:-3]
            self.filenames_and_header.append((entry, header))
            parts = split_path(entry)

            # For page title manager, we don't need to pass header since it will
            # extract the title from the cache or file content
            title_manager = get_page_title_manager()
            if title_manager and title_manager.enabled:
                # Let the title manager handle title extraction
                self.add_node(self.tree, [], parts, None)
            else:
                # Original behavior with header
                self.add_node(self.tree, [], parts, header)
        app.logger.debug(
            f"SidebarPageIndex.load({path}) reading entries, adding nodes took {timer() - t_start:.3f} seconds."
        )

    def query(self):
        return self.tree
