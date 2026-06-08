using Autodesk.Revit.UI;
using System.Text.Json.Nodes;

namespace RevitWriteServer.Commands;

/// <summary>
/// Thread-safe base for all Revit commands.
///
/// Revit API calls MUST run on the UI thread.  This base class marshals every
/// command through an ExternalEvent, blocks the TCP thread until the result is
/// ready, then returns.
///
/// Derived classes implement:
///   PrepareParameters(JsonNode?) — parse the JSON params before raising the event
///   ExecuteOnRevitThread(Document) — Revit API work; runs inside a Transaction
///   GetResult() — return the serialisable result object to the caller
/// </summary>
public abstract class CommandBase<TResult> : IRevitCommand, IExternalEventHandler
{
    protected readonly UIApplication UiApp;
    private readonly ExternalEvent _externalEvent;
    private readonly SemaphoreSlim _gate = new(1, 1);      // one call at a time
    private readonly ManualResetEventSlim _done = new(false);
    private TResult? _result;
    private Exception? _error;

    public abstract string CommandName { get; }

    protected CommandBase(UIApplication uiApp)
    {
        UiApp = uiApp;
        _externalEvent = ExternalEvent.Create(this);
    }

    // ── Called from the TCP background thread ─────────────────────────────────

    public object Execute(JsonNode? parameters, string requestId)
    {
        _gate.Wait();          // serialise concurrent calls to the same command
        try
        {
            _done.Reset();
            _error = null;
            PrepareParameters(parameters);

            var status = _externalEvent.Raise();
            if (status != ExternalEventRequest.Accepted)
                throw new InvalidOperationException($"ExternalEvent not accepted: {status}");

            if (!_done.Wait(TimeSpan.FromSeconds(30)))
                throw new TimeoutException($"Command '{CommandName}' timed out after 30 s");

            if (_error is not null)
                throw _error;

            return GetResult()!;
        }
        finally
        {
            _gate.Release();
        }
    }

    // ── Direct execution (for WPF button handlers already on the UI thread) ───
    //
    // ExternalEvent is only needed to MARSHAL onto the UI thread.
    // When a WPF event handler fires, we ARE on the Revit/WPF UI thread, so we
    // can call Revit API directly — no ExternalEvent required.

    public object RunOnUIThread(JsonNode? parameters, Autodesk.Revit.DB.Document doc)
    {
        _error  = null;
        _result = default;
        PrepareParameters(parameters);
        ExecuteOnRevitThread(doc);
        if (_error is not null) throw _error;
        return GetResult()!;
    }

    // ── Called by Revit on the UI thread ─────────────────────────────────────

    void IExternalEventHandler.Execute(UIApplication app)
    {
        try
        {
            var doc = app.ActiveUIDocument?.Document
                ?? throw new InvalidOperationException("No active Revit document.");
            ExecuteOnRevitThread(doc);
        }
        catch (Exception ex)
        {
            _error = ex;
        }
        finally
        {
            _done.Set();
        }
    }

    string IExternalEventHandler.GetName() => CommandName;

    // ── Subclass contract ─────────────────────────────────────────────────────

    protected abstract void PrepareParameters(JsonNode? parameters);
    protected abstract void ExecuteOnRevitThread(Autodesk.Revit.DB.Document doc);
    protected abstract object GetResult();

    protected TResult? Result
    {
        get => _result;
        set => _result = value;
    }
}
