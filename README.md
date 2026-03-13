PROCEDURE Execute_Engineering_Reasoning_Loop(IncomingEvent)

    CurrentContext <- Retrieve_Agent_Memory()
    RepoState <- Inspect_Repository_Status()
    TestResults <- Fetch_Recent_Test_Outcomes()

    PromptInput <- Combine_Data(IncomingEvent, CurrentContext, RepoState, TestResults)

    ReasoningResponse <- Prompt_LLM(PromptInput, "JSON_Format")

    ParsedPlan <- Extract_Plan(ReasoningResponse)

    Save_To_Memory(ParsedPlan.Thought)

    ActionResults <- Initialize_Empty_List()

    FOR EACH Action IN ParsedPlan.Actions DO

        SWITCH Action.Name DO

            CASE "GenerateCode":
                Result <- Generate_Code_From_Spec(Action.Parameters.Spec)
                Append Result TO ActionResults

            CASE "WriteFile":
                Result <- Write_Code_To_Repository(
                              Action.Parameters.FilePath,
                              Action.Parameters.Content
                          )
                Append Result TO ActionResults

            CASE "RunTests":
                Result <- Execute_Test_Suite(Action.Parameters.TestScope)
                Append Result TO ActionResults

            CASE "FixBugs":
                Result <- Apply_Auto_Fixes(Action.Parameters.BugList)
                Append Result TO ActionResults

            CASE "CommitChanges":
                Result <- Commit_To_Git(
                              Action.Parameters.CommitMessage
                          )
                Append Result TO ActionResults

            CASE "Deploy":
                Result <- Trigger_Deployment(Action.Parameters.Target)
                Append Result TO ActionResults

            CASE "ReportStatus":
                Result <- Generate_Engineering_Report(Action.Parameters)
                Append Result TO ActionResults

            DEFAULT:
                Result <- Log_Unknown_Action(Action.Name)
                Append Result TO ActionResults

        END SWITCH

    END FOR

    IF Requires_Further_Reasoning(ActionResults) IS TRUE THEN
        RETURN Execute_Engineering_Reasoning_Loop(ActionResults)
    END IF

    Update_Engineering_Dashboard("Idle", ParsedPlan.Thought)

    FinalResponse <- Prompt_LLM_For_Response(ActionResults)

    Save_To_Memory(IncomingEvent, FinalResponse)

    RETURN FinalResponse

END PROCEDURE
Starting of ReadMe for Tesla STEM Enterprise project.
