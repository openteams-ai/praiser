from praiser.extractors.enhancement_proposals import (
    guess_format,
    looks_like_proposal_dir,
    parse_authors,
    parse_proposal_header,
)

PEP_RFC2822 = """\
PEP: 8
Title: Style Guide for Python Code
Author: Guido van Rossum <guido@python.org>,
        Barry Warsaw <barry@python.org>,
        Nick Coghlan <ncoghlan@gmail.com>
Status: Active
Type: Process

Introduction
============
This document gives coding conventions.
"""

NEP_RST_FIELDLIST = """\
.. _NEP01:

================================
NEP 1 — A Simple Way to Propose
================================

:Author: Jarrod Millman <millman@berkeley.edu>
:Status: Active
:Type: Process

Abstract
--------
Body text here.
"""

JEP_YAML = """\
---
title: My Proposal
author:
  - Jane Roe (@janeroe)
  - John Doe <john@doe.io>
status: Accepted
---

# My Proposal
"""


def test_parse_rfc2822_header_with_continuation():
    h = parse_proposal_header(PEP_RFC2822, "rst")
    assert h["title"] == "Style Guide for Python Code"
    assert "Guido van Rossum" in h["author"]
    assert "Nick Coghlan" in h["author"]  # continuation lines merged


def test_parse_rst_fieldlist_header():
    h = parse_proposal_header(NEP_RST_FIELDLIST, "rst")
    assert "Jarrod Millman" in h["author"]
    assert h["status"] == "Active"


def test_parse_yaml_frontmatter():
    h = parse_proposal_header(JEP_YAML, "yaml")
    assert isinstance(h["author"], list)
    assert len(h["author"]) == 2


def test_parse_authors_splits_and_extracts():
    authors = parse_authors(parse_proposal_header(PEP_RFC2822, "rst")["author"])
    names = {a.name for a in authors}
    emails = {a.email for a in authors}
    assert "Guido van Rossum" in names
    assert "guido@python.org" in emails
    assert len(authors) == 3


def test_parse_authors_handle_and_email():
    authors = parse_authors(parse_proposal_header(JEP_YAML, "yaml")["author"])
    handles = {a.handle for a in authors}
    emails = {a.email for a in authors}
    assert "janeroe" in handles
    assert "john@doe.io" in emails


def test_looks_like_proposal_dir():
    assert looks_like_proposal_dir(
        ["pep-0008.rst", "pep-0020.rst", "pep-0257.rst", "README.rst"]
    )
    assert not looks_like_proposal_dir(["README.md", "index.md"])


def test_guess_format():
    assert guess_format(["pep-0008.rst", "pep-0020.rst"]) == "rst"
    assert guess_format(["0001-intro.md", "0002-next.md"]) == "yaml"
