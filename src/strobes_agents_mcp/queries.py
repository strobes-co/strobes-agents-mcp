"""GraphQL documents for the Strobes public endpoint (strobes/graphql/public/agents).

Mutation documents must contain the mutation field name verbatim — the backend's
MasterKey allowlist matches on it.
"""

WORKSPACE = """
  id name description status workspaceType templateSlug supervisorMode
  engagementId engagementName createdByName createdByEmail createdAt updatedAt
"""
STATS = """
  chatCount tableCount taskCount activeTaskCount completedTaskCount skillCount
  learningCount creditsConsumed assetCount findingCount reportCount
"""
TASK = """
  id title instructions taskType agentType status errorMessage createdAt
  startedAt completedAt durationSeconds createdByType
"""
TASK_WITH_DATA = TASK + " outputData inputData"

WORKSPACES = f"""
query Workspaces($org: UUID!, $status: String, $engagement: UUID, $limit: Int, $offset: Int) {{
  workspaces(organizationId: $org, status: $status, engagementId: $engagement,
             limit: $limit, offset: $offset) {{ {WORKSPACE} }}
}}"""

WORKSPACE_GET = f"""
query Workspace($id: UUID!) {{
  workspace(id: $id) {{ {WORKSPACE} customInstructions stats {{ {STATS} }} }}
}}"""

WORKSPACE_STATUS = """
query WorkspaceStatus($id: UUID!) { workspace(id: $id) { id name status } }"""

TASKS = f"""
query WorkspaceTasks($ws: UUID!, $status: String) {{
  workspaceTasks(workspaceId: $ws, status: $status) {{ {TASK} }}
}}"""

TASKS_WITH_DATA = f"""
query WorkspaceTasksFull($ws: UUID!, $status: String) {{
  workspaceTasks(workspaceId: $ws, status: $status) {{ {TASK_WITH_DATA} }}
}}"""

TASK_MESSAGES = """
query TaskMessages($ws: UUID!, $task: UUID!, $limit: Int, $offset: Int) {
  workspaceTaskMessages(workspaceId: $ws, taskId: $task, limit: $limit, offset: $offset) {
    id author agentId agentName blocks status createdAt
  }
}"""

FINDINGS = """
query WorkspaceFindings($ws: UUID!, $page: Int, $size: Int, $search: String,
                        $severity: [Int], $state: [Int]) {
  workspaceFindings(workspaceId: $ws, page: $page, pageSize: $size, search: $search,
                    severity: $severity, state: $state) {
    id title severity severityLabel assetId assetName state stateLabel created
  }
}"""

ASSETS = """
query WorkspaceAssets($ws: UUID!, $page: Int, $size: Int, $search: String) {
  workspaceAssets(workspaceId: $ws, page: $page, pageSize: $size, search: $search) {
    id name type typeLabel target sensitivity sensitivityLabel created
  }
}"""

FILES = """
query WorkspaceFiles($ws: UUID!, $path: String, $recursive: Boolean, $search: String) {
  workspaceFiles(workspaceId: $ws, path: $path, recursive: $recursive, search: $search) {
    name path isFolder size lastModified contentType
  }
}"""

FILE_URL = """
query FileUrl($ws: UUID!, $path: String!) {
  workspaceFileDownloadUrl(workspaceId: $ws, path: $path)
}"""

ZIP_URL = """
query ZipUrl($ws: UUID!, $path: String) {
  workspaceDownloadUrl(workspaceId: $ws, path: $path)
}"""

TEMPLATES = """
query WorkflowTemplates {
  workflowTemplates { slug name description version phaseCount requiredVariables }
}"""

WORKFLOW = """
query WorkspaceWorkflow($ws: UUID!, $wf: UUID) {
  workspaceWorkflow(workspaceId: $ws, workflowId: $wf) {
    workflowId name templateSlug templateVersion status currentPhaseKey variables
    blockedReason awaitingInput totalTasks completedTasks startedAt completedAt createdAt
    phases {
      phaseKey phaseName stage order status gateType failurePolicy iterationCount
      awaitingInput startedAt completedAt
      tasks {
        taskId taskTemplateKey title status executionKind startedAt completedAt
        blockedReason errorMessage
        awaitingInput { interruptId title message interruptType agentName requestedAt }
      }
    }
  }
}"""

AGENTS = """
query AvailableAgents($org: UUID!) {
  availableAgents(organizationId: $org) { id name description role source capabilities }
}"""

# ------------------------------------------------------------------ mutations
CREATE_WORKSPACE = f"""
mutation CreateWorkspace($org: UUID!, $name: String, $engagement: UUID, $settings: GenericScalar) {{
  createWorkspace(organizationId: $org, name: $name, engagementId: $engagement, settings: $settings) {{
    workspace {{ {WORKSPACE} }} setupThread {{ id }}
  }}
}}"""

SKIP_SETUP = f"""
mutation SkipWorkspaceSetup($ws: UUID!) {{
  skipWorkspaceSetup(workspaceId: $ws) {{ success workspace {{ id status }} }}
}}"""

ARCHIVE_WORKSPACE = """
mutation ArchiveWorkspace($ws: UUID!) {
  archiveWorkspace(workspaceId: $ws) { success workspace { id status } }
}"""

CREATE_FROM_TEMPLATE = f"""
mutation CreateWorkspaceFromTemplate($org: UUID!, $slug: String!, $vars: GenericScalar!,
    $name: String, $engagement: UUID, $engagementMode: Boolean, $startDate: Date) {{
  createWorkspaceFromTemplate(organizationId: $org, templateSlug: $slug, variables: $vars,
      name: $name, engagementId: $engagement, engagementMode: $engagementMode,
      startDate: $startDate) {{
    workspace {{ {WORKSPACE} }} setupThread {{ id }}
  }}
}}"""

CREATE_TASK = f"""
mutation CreateWorkspaceTask($ws: UUID!, $title: String!, $instructions: String!,
    $taskType: String!, $agentType: String!, $input: GenericScalar) {{
  createWorkspaceTask(workspaceId: $ws, title: $title, instructions: $instructions,
      taskType: $taskType, agentType: $agentType, inputData: $input) {{ task {{ {TASK} }} }}
}}"""

CANCEL_TASK = """
mutation CancelWorkspaceTask($ws: UUID!, $task: UUID!) {
  cancelWorkspaceTask(workspaceId: $ws, taskId: $task) { success task { id status } }
}"""

RETRY_TASK = """
mutation RetryWorkspaceTask($ws: UUID!, $task: UUID!) {
  retryWorkspaceTask(workspaceId: $ws, taskId: $task) { success task { id status } }
}"""

PAUSE_WORKFLOW = """
mutation PauseWorkflow($ws: UUID!, $wf: UUID) {
  pauseWorkflow(workspaceId: $ws, workflowId: $wf) { success }
}"""

RESUME_WORKFLOW = """
mutation ResumeWorkflow($ws: UUID!, $wf: UUID) {
  resumeWorkflow(workspaceId: $ws, workflowId: $wf) { success }
}"""

CANCEL_WORKFLOW = """
mutation CancelWorkflow($ws: UUID!, $wf: UUID) {
  cancelWorkflow(workspaceId: $ws, workflowId: $wf) { success }
}"""

RESTART_WORKFLOW = """
mutation RestartWorkflow($ws: UUID!, $wf: UUID) {
  restartWorkflow(workspaceId: $ws, workflowId: $wf) { success }
}"""

RESTART_WORKFLOW_FROM_PHASE = """
mutation RestartWorkflowFromPhase($ws: UUID!, $wf: UUID, $phase: String!, $scope: String) {
  restartWorkflowFromPhase(workspaceId: $ws, workflowId: $wf, phaseKey: $phase, scope: $scope) {
    success
  }
}"""

ADVANCE_WORKFLOW_PHASE = """
mutation AdvanceWorkflowPhase($ws: UUID!, $wf: UUID) {
  advanceWorkflowPhase(workspaceId: $ws, workflowId: $wf) { success }
}"""
