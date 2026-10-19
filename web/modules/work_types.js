// Browser twin of ouroboros/gateway/work_contracts.py: the /api/state work projection (v1, additive).
// One row per work (a request with its Continue episodes and live members) and one status per room.
// Words come from the server's codes through tr(); a client never derives a state, mark or control.

/**
 * @typedef {Object} WorkReason
 * @property {string} code  // closed per release; an unknown code renders its text
 * @property {string} text  // the English sentence for this code
 * @property {number=} count
 * @property {Array<string>=} ids
 * @property {*=} since
 */
/**
 * @typedef {Object} WorkTurn
 * @property {string} kind  // question | action
 * @property {string} task_id
 * @property {string=} quiz_id
 * @property {string=} wait_id
 * @property {string=} code
 */
/**
 * @typedef {Object} WorkControl
 * @property {string} action  // stop | stop_earlier | pause | resume | hurry | wrap_up
 * @property {string} target  // the exact task id the door acts on
 * @property {string=} scope  // single | cascade | work
 */
/**
 * @typedef {Object} WorkEpisode
 * @property {string} task_id
 * @property {boolean} live
 */
/**
 * @typedef {Object} WorkMembers
 * @property {number} working
 * @property {number} waiting
 * @property {number} delegated
 * @property {Array<string>} ids  // every member task id (episode roots and helpers): a card of another id re-reads the state
 */
/**
 * @typedef {Object} WorkFocus
 * @property {string} text
 * @property {*} at
 */
/**
 * @typedef {Object} Work
 * @property {string} work_id
 * @property {*} room
 * @property {string} name
 * @property {string} kind  // direct | task
 * @property {string} project_id
 * @property {WorkEpisode} episode
 * @property {string} activity  // working | queued | waiting | paused | unknown
 * @property {string} transition  // none | finalizing | pausing
 * @property {string} call  // preparing | sending | thinking | working, when every working member agrees
 * @property {Array<WorkReason>} because
 * @property {Array<WorkTurn>} turn
 * @property {string} release  // self | owner | mind | none
 * @property {?string} paused_by  // owner | mind | host
 * @property {WorkMembers} members
 * @property {Array<WorkControl>} controls
 * @property {*} since
 * @property {boolean} confirmed
 * @property {string=} outcome
 * @property {WorkFocus=} focus
 * @property {Array<Object>=} gaps
 * @property {string=} client_message_id
 */
/**
 * @typedef {Object} RoomStatus
 * @property {string} activity  // working | queued | waiting | paused | idle | unknown
 * @property {string} transition
 * @property {string} call
 * @property {Array<Object>} turn
 * @property {boolean} amber
 * @property {string} mark  // moving | still | dot | ring | none
 * @property {boolean} confirmed
 * @property {Array<string>} works
 */
/**
 * @typedef {Object} WorkSummary
 * @property {Object<string, number>} counts
 * @property {number} turn
 * @property {boolean} confirmed
 */

export {};
