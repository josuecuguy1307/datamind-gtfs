# Commercial licensing

This page is a plain-language guide, not legal advice. The text of the
[LICENSE](../LICENSE) is what counts. That text is currently a draft that has not
been reviewed by a lawyer.

## You can use and modify ML DataMind freely inside your organization

Under the ML DataMind Sustainable Use License you may read the code, copy it,
modify it and run it, as long as you are using it for your own purposes. You may
also adapt and extend it, keep **private forks**, and use those modified
versions internally. None of that needs a commercial license, and you do not have
to publish your changes. That includes:

- a **municipality**, **transit operator**, company or **consultancy** installing
  it to process its own data;
- a **university** or research group using it for research and teaching;
- people building **integrations** and tools around it;
- anyone **contributing** improvements to the project.

You can also publish and use the data and reports it produces (subject to the
licenses of the underlying data, such as OpenStreetMap's ODbL), **sell GTFS feeds
and datasets** made with it, build **apps that consume them**, and charge for
consulting, training and implementation work around it.

## Example: a consultancy

A consultancy can run ML DataMind to process a client's data, charge for that
work, and hand the client the GTFS feeds, datasets, reports and other outputs it
produces, with no commercial license. It can also install and configure ML
DataMind on the client's own systems for the client's internal use, charging for
its time and not for the software.

It would need a commercial license only if it gave the client **hosted access**
to ML DataMind, sold access to ML DataMind itself, or turned it into a SaaS,
white-label or resale offering.

## Apps and services built around ML DataMind

- An app that **consumes GTFS feeds, datasets or other outputs** already generated
  with ML DataMind is fine.
- An **internal app** of an organization may call ML DataMind live for that
  organization's own internal users (its people and those working for it under
  contract).
- If an app or service lets **customers or other outside users** invoke ML
  DataMind's functionality on demand, directly or indirectly, that counts as a
  hosted offering and needs a commercial license.

## What is never restricted

- Using ML DataMind's **outputs** does not make an app a competitor.
- **Writing your own system from scratch**, with your own code, even one that does
  something similar to ML DataMind.
- **Integrations** and plugins around it, and anything you built using what you
  learned from it.

## Talk to us if you want to

- offer ML DataMind as a **hosted service or SaaS**, or give third parties access
  to its functionality, including by letting your customers invoke it on demand
  through your app or service;
- **resell** it, or ship it **white-label** under another name;
- offer a **fork or a substantially modified version** of ML DataMind as a
  commercial product or service;
- use ML DataMind's code, or a substantial modification of its engine, as the
  **core of a product that competes directly** with ML DataMind;
- set up a **partnership** or an **OEM** license.

In all of these, ML DataMind itself, or a substantial modification of its code, is
what gets offered to others. They need a separate written license.

## How to reach us

Write to **josuearcos1307@gmail.com** and tell us:

1. who you are and what your organization does;
2. what you want to build or offer;
3. who would use it, and roughly how.

We read every message. There is no price list here, because each case is
different.

## Earlier versions

Versions published before the license change (through commit `33d2b20`) stay under
the MIT License. See [`docs/legal/VERSIONS-AND-LICENSES.md`](legal/VERSIONS-AND-LICENSES.md).
