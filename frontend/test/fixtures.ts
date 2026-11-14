// The shared fixtures, validated against the shapes before they are used as typed data.
import type { AiStatus, Catalog, FoldersResponse, PlanDocument, ProjectState, StatusResponse, Waveform } from "../src/api/types";
import catalogJson from "../../tests/fixtures/editor/catalog.json";
import foldersJson from "../../tests/fixtures/editor/folders.json";
import planDocumentJson from "../../tests/fixtures/editor/plan_document.json";
import projectNewJson from "../../tests/fixtures/editor/project_new.json";
import projectPlannedJson from "../../tests/fixtures/editor/project_planned.json";
import statusDoneJson from "../../tests/fixtures/editor/status_done.json";
import waveformJson from "../../tests/fixtures/editor/waveform.json";
import * as shapes from "./shapes";

function typed<T>(spec: shapes.Spec, value: unknown, name: string): T {
  const problems = shapes.problemsOf(spec, value, name);
  if (problems.length > 0) throw new Error(`fixture ${name} does not match its type:\n${problems.join("\n")}`);
  return value as T;
}

export const planDocument = typed<PlanDocument>(shapes.planDocument, planDocumentJson, "plan_document");
export const catalog = typed<Catalog>(shapes.catalog, catalogJson, "catalog");
export const folders = typed<FoldersResponse>(shapes.folders, foldersJson, "folders");
export const projectNew = typed<ProjectState>(shapes.projectState, projectNewJson, "project_new");
export const projectPlanned = typed<ProjectState>(shapes.projectState, projectPlannedJson, "project_planned");
export const statusDone = typed<StatusResponse>(shapes.status, statusDoneJson, "status_done");
export const waveform = typed<Waveform>(shapes.waveform, waveformJson, "waveform");
export const aiAvailable: AiStatus = { available: true, reachable: true, model: "qwen3-vl:4b", model_present: true, reason: null };

/** A copy with something changed, for the cases the fixtures do not cover. */
export const clone = <T>(value: T): T => structuredClone(value);
